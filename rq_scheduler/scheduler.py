import logging
import signal
import time

from datetime import datetime
from itertools import repeat

from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.queue import Queue

from redis import WatchError

from .utils import from_unix, to_unix, get_next_scheduled_time, rationalize_until

LOGGING = {
    'version': 1,
    'formatters': {
        'default': {
            'format': '[%(asctime)s][%(process)d][%(funcName)s][%(message)s]',
        },
    },
    'handlers': {
        'default': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'default',
        },
    },
    'loggers': {
        'rq_scheduler': {
            'handlers': ['default'],
            'propagate': True,
            'level': 'DEBUG',
        },
    },
}

import logging.config
logging.config.dictConfig(LOGGING)
DEFAULT_LOGGER = logging.getLogger(__name__)


class Scheduler(object):
    scheduler_key = 'rq:scheduler'
    scheduled_jobs_key = 'rq:scheduler:scheduled_jobs'

    def __init__(self, queue_name='default', interval=60, connection=None, logger=DEFAULT_LOGGER):
        from rq.connections import resolve_connection
        self.connection = resolve_connection(connection)
        self.queue_name = queue_name
        self._interval = interval
        self.log = logger
        self._lock_acquired = False

    def register_birth(self):
        if self.connection.exists(self.scheduler_key) and \
                not self.connection.hexists(self.scheduler_key, 'death'):
            raise ValueError("There's already an active RQ scheduler")

        key = self.scheduler_key
        now = time.time()

        with self.connection._pipeline() as p:
            p.delete(key)
            p.hset(key, 'birth', now)
            # Set scheduler key to expire a few seconds after polling interval
            # This way, the key will automatically expire if scheduler
            # quits unexpectedly
            p.expire(key, int(self._interval) + 10)
            p.execute()

    def register_death(self):
        """Registers its own death."""
        with self.connection._pipeline() as p:
            p.hset(self.scheduler_key, 'death', time.time())
            p.expire(self.scheduler_key, 60)
            p.execute()

    def acquire_lock(self):
        """
        Acquire lock before scheduling jobs to prevent another scheduler
        from scheduling jobs at the same time.

        This function returns True if a lock is acquired. False otherwise.
        """
        key = '%s_lock' % self.scheduler_key
        now = time.time()
        expires = int(self._interval) + 10
        self._lock_acquired = self.connection.set(
                key, now, ex=expires, nx=True)
        return self._lock_acquired

    def remove_lock(self):
        """
        Remove acquired lock.
        """
        key = '%s_lock' % self.scheduler_key

        if self._lock_acquired:
            self.connection.delete(key)

    def _install_signal_handlers(self):
        """
        Installs signal handlers for handling SIGINT and SIGTERM
        gracefully.
        """

        def stop(signum, frame):
            """
            Register scheduler's death and exit
            and remove previously acquired lock and exit.
            """
            self.log.error('Shutting down RQ scheduler...')
            self.register_death()
            self.remove_lock()
            raise SystemExit()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

    def _create_job(self, func, args=None, kwargs=None, commit=True,
                    result_ttl=None, ttl=None, id=None, description=None,
                    queue_name=None, timeout=None):
        """
        Creates an RQ job and saves it to Redis.
        """
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        job = Job.create(func, args=args, connection=self.connection,
                         kwargs=kwargs, result_ttl=result_ttl, ttl=ttl, id=id,
                         description=description, timeout=timeout)
        job.origin = queue_name or self.queue_name
        if commit:
            self.log.error('about to set redis key: %s', job.id)
            job.save()
        return job

    def enqueue_at(self, scheduled_time, func, *args, **kwargs):
        """
        Pushes a job to the scheduler queue. The scheduled queue is a Redis sorted
        set ordered by timestamp - which in this case is job's scheduled execution time.

        Usage:

        from datetime import datetime
        from redis import Redis
        from rq.scheduler import Scheduler

        from foo import func

        redis = Redis()
        scheduler = Scheduler(queue_name='default', connection=redis)
        scheduler.enqueue_at(datetime(2020, 1, 1), func, 'argument', keyword='argument')
        """
        timeout = kwargs.pop('timeout', None)
        job_id = kwargs.pop('job_id', None)
        job_ttl = kwargs.pop('job_ttl', None)
        job_result_ttl = kwargs.pop('job_result_ttl', None)

        job = self._create_job(func, args=args, kwargs=kwargs, timeout=timeout,
                               id=job_id, result_ttl=job_result_ttl, ttl=job_ttl)
        self.log.error('about to add key into %s: %s', self.scheduled_jobs_key, job.id)
        self.connection._zadd(self.scheduled_jobs_key,
                              to_unix(scheduled_time),
                              job.id)
        return job

    def enqueue_in(self, time_delta, func, *args, **kwargs):
        """
        Similar to ``enqueue_at``, but accepts a timedelta instead of datetime object.
        The job's scheduled execution time will be calculated by adding the timedelta
        to datetime.utcnow().
        """
        timeout = kwargs.pop('timeout', None)
        job_id = kwargs.pop('job_id', None)
        job_ttl = kwargs.pop('job_ttl', None)
        job_result_ttl = kwargs.pop('job_result_ttl', None)

        job = self._create_job(func, args=args, kwargs=kwargs, timeout=timeout,
                               id=job_id, result_ttl=job_result_ttl, ttl=job_ttl)
        self.log.error('about to add key into %s: %s', self.scheduled_jobs_key, job.id)
        self.connection._zadd(self.scheduled_jobs_key,
                              to_unix(datetime.utcnow() + time_delta),
                              job.id)
        return job

    def schedule(self, scheduled_time, func, args=None, kwargs=None,
                 interval=None, repeat=None, result_ttl=None, ttl=None,
                 timeout=None, id=None, description=None, queue_name=None):
        """
        Schedule a job to be periodically executed, at a certain interval.
        """
        # Set result_ttl to -1 for periodic jobs, if result_ttl not specified
        if interval is not None and result_ttl is None:
            result_ttl = -1
        job = self._create_job(func, args=args, kwargs=kwargs, commit=False,
                               result_ttl=result_ttl, ttl=ttl, id=id,
                               description=description, queue_name=queue_name,
                               timeout=timeout)

        if interval is not None:
            job.meta['interval'] = int(interval)
        if repeat is not None:
            job.meta['repeat'] = int(repeat)
        if repeat and interval is None:
            raise ValueError("Can't repeat a job without interval argument")
        self.log.error('about to set redis key: %s', job.id)
        job.save()
        self.log.error('about to add key into %s: %s', self.scheduled_jobs_key, job.id)
        self.connection._zadd(self.scheduled_jobs_key,
                              to_unix(scheduled_time),
                              job.id)
        return job

    def cron(self, cron_string, func, args=None, kwargs=None, repeat=None,
             queue_name=None, id=None, timeout=None, description=None):
        """
        Schedule a cronjob
        """
        scheduled_time = get_next_scheduled_time(cron_string)

        # Set result_ttl to -1, as jobs scheduled via cron are periodic ones.
        # Otherwise the job would expire after 500 sec.
        job = self._create_job(func, args=args, kwargs=kwargs, commit=False,
                               result_ttl=-1, id=id, queue_name=queue_name,
                               description=description, timeout=timeout)

        job.meta['cron_string'] = cron_string

        if repeat is not None:
            job.meta['repeat'] = int(repeat)

        self.log.error('about to set redis key: %s', job.id)
        job.save()

        self.log.error('about to add key into %s: %s', self.scheduled_jobs_key, job.id)
        self.connection._zadd(self.scheduled_jobs_key,
                              to_unix(scheduled_time),
                              job.id)
        return job

    def cancel(self, job):
        """
        Pulls a job from the scheduler queue. This function accepts either a
        job_id or a job instance.
        """
        if isinstance(job, Job):
            self.log.error('about to del job from %s: %s', self.scheduled_jobs_key, job.id)
            self.connection.zrem(self.scheduled_jobs_key, job.id)
        else:
            self.log.error('about to del job from %s: %s', self.scheduled_jobs_key, job)
            self.connection.zrem(self.scheduled_jobs_key, job)

    def __contains__(self, item):
        """
        Returns a boolean indicating whether the given job instance or job id
        is scheduled for execution.
        """
        job_id = item
        if isinstance(item, Job):
            job_id = item.id
        return self.connection.zscore(self.scheduled_jobs_key, job_id) is not None

    def change_execution_time(self, job, date_time):
        """
        Change a job's execution time.
        """
        with self.connection._pipeline() as pipe:
            while 1:
                try:
                    pipe.watch(self.scheduled_jobs_key)
                    if pipe.zscore(self.scheduled_jobs_key, job.id) is None:
                        raise ValueError('Job not in scheduled jobs queue')
                    pipe.zadd(self.scheduled_jobs_key, to_unix(date_time), job.id)
                    break
                except WatchError:
                    # If job is still in the queue, retry otherwise job is already executed
                    # so we raise an error
                    if pipe.zscore(self.scheduled_jobs_key, job.id) is None:
                        raise ValueError('Job not in scheduled jobs queue')
                    continue

    def count(self, until=None):
        """
        Returns the total number of jobs that are scheduled for all queues.
        This function accepts datetime, timedelta instances as well as
        integers representing epoch values.
        """

        until = rationalize_until(until)
        return self.connection.zcount(self.scheduled_jobs_key, 0, until)

    def get_jobs(self, until=None, with_times=False, offset=None, length=None):
        """
        Returns a list of job instances that will be queued until the given
        time. If no 'until' argument is given all jobs are returned.

        If with_times is True, a list of tuples consisting of the job instance
        and it's scheduled execution time is returned.

        If offset and length are specified, a slice of the list starting at the
        specified zero-based offset of the specified length will be returned.

        If either of offset or length is specified, then both must be, or
        an exception will be raised.
        """
        def epoch_to_datetime(epoch):
            return from_unix(float(epoch))

        until = rationalize_until(until)
        job_ids = self.connection.zrangebyscore(self.scheduled_jobs_key, 0,
                                                until, withscores=with_times,
                                                score_cast_func=epoch_to_datetime,
                                                start=offset, num=length)
        self.log.error('got jobs until %s from redis zset %s: %s', until, self.scheduled_jobs_key, job_ids)
        if not with_times:
            job_ids = zip(job_ids, repeat(None))
        jobs = []
        for job_id, sched_time in job_ids:
            job_id = job_id.decode('utf-8')
            try:
                job = Job.fetch(job_id, connection=self.connection)
                if with_times:
                    jobs.append((job, sched_time))
                else:
                    jobs.append(job)
            except NoSuchJobError:
                # Delete jobs that aren't there from scheduler
                self.log.error('redis key no found, about to delete job %s', job_id)
                self.cancel(job_id)
        return jobs

    def get_jobs_to_queue(self, with_times=False):
        """
        Returns a list of job instances that should be queued
        (score lower than current timestamp).
        If with_times is True a list of tuples consisting of the job instance and
        it's scheduled execution time is returned.
        """
        return self.get_jobs(to_unix(datetime.utcnow()), with_times=with_times)

    def get_queue_for_job(self, job):
        """
        Returns a queue to put job into.
        """
        key = '{0}{1}'.format(Queue.redis_queue_namespace_prefix, job.origin)
        return Queue.from_queue_key(key, connection=self.connection)

    def enqueue_job(self, job):
        """
        Move a scheduled job to a queue. In addition, it also does puts the job
        back into the scheduler if needed.
        """
        self.log.error('Pushing {0} to {1}'.format(job.id, job.origin))

        interval = job.meta.get('interval', None)
        repeat = job.meta.get('repeat', None)
        cron_string = job.meta.get('cron_string', None)

        # If job is a repeated job, decrement counter
        if repeat:
            job.meta['repeat'] = int(repeat) - 1

        queue = self.get_queue_for_job(job)
        self.log.error('about to enqueue job %s', job.id)
        queue.enqueue_job(job)
        self.connection.zrem(self.scheduled_jobs_key, job.id)
        self.log.error('about to delete job %s', job.id)

        if interval:
            # If this is a repeat job and counter has reached 0, don't repeat
            if repeat is not None:
                if job.meta['repeat'] == 0:
                    return
            self.connection._zadd(self.scheduled_jobs_key,
                                  to_unix(datetime.utcnow()) + int(interval),
                                  job.id)
        elif cron_string:
            # If this is a repeat job and counter has reached 0, don't repeat
            if repeat is not None:
                if job.meta['repeat'] == 0:
                    return
            self.log.error('about to zadd the same cronjob %s', job.id)
            self.connection._zadd(self.scheduled_jobs_key,
                                  to_unix(get_next_scheduled_time(cron_string)),
                                  job.id)

    def enqueue_jobs(self):
        """
        Move scheduled jobs into queues.
        """
        self.log.error('Checking for scheduled jobs...')

        jobs = self.get_jobs_to_queue()
        for job in jobs:
            self.enqueue_job(job)

        # Refresh scheduler key's expiry
        self.connection.expire(self.scheduler_key, int(self._interval) + 10)
        return jobs

    def run(self, burst=False):
        """
        Periodically check whether there's any job that should be put in the queue (score
        lower than current time).
        """
        self.log.error('Running RQ scheduler...')

        self.register_birth()
        self._install_signal_handlers()

        try:
            while True:

                start_time = time.time()
                if self.acquire_lock():
                    self.log.error('about to check if there is something to enqueue')
                    self.enqueue_jobs()

                    if burst:
                        self.log.error('RQ scheduler done, quitting')
                        break
                else:
                    self.log.error('Waiting for lock...')

                # Time has already elapsed while enqueuing jobs, so don't wait too long.
                time.sleep(self._interval - (time.time() - start_time))
        finally:
            self.remove_lock()
            self.register_death()
