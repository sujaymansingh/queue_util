"""Listens to 1 (just one!) queue and consumes messages from it endlessly.
We set up a consumer with two things:
1) The name of the source queue (`source_queue_name`)
2) A callable that will process

The `handle_data` method must process the data. It can return nothing or a
sequence of `queue_name, data` pairs.
If it returns the latter, then the data will be sent to the given `queue_name`.
e.g.

def handle_data(data):
    new_data = do_some_calc(data)

    # Forward the new_data to another queue.
    #
    yield ("next_target", new_data)
"""
import logging
import os
import socket
import time

import kombu
import statsd

from queue_util import stats


class Consumer(object):

    def __init__(self, source_queue_name, handle_data, rabbitmq_host, serializer=None, compression=None, pause_delay=5, statsd_host=None, statsd_prefix="queue_util", workerid=None, worker_id=None, dont_requeue=None, reject=None, handle_exception=None):
        self.serializer = serializer
        self.compression = compression
        self.queue_cache = {}

        self.pause_delay = pause_delay

        # Connect to the source queue.
        #
        self.broker = kombu.BrokerConnection(rabbitmq_host)
        self.source_queue = self.get_queue(source_queue_name, serializer=serializer, compression=compression)

        # The handle_data method will be applied to each item in the queue.
        #
        self.handle_data = handle_data

        self.workerid = worker_id or workerid

        # If both True, requeue takes priority
        self.requeue = False if dont_requeue else True
        self.reject = True if reject else False

        self.handle_exception = handle_exception

        if statsd_host:
            prefix = self.get_full_statsd_prefix(statsd_prefix, source_queue_name)
            self.statsd_client = statsd.StatsClient(statsd_host, prefix=prefix)
        else:
            self.statsd_client = None

    def get_queue(self, queue_name, serializer=None, compression=None):
        kwargs = {}

        # Use 'defaults' if no args were supplied for serializer/compression.
        #
        serializer = serializer or self.serializer
        if serializer:
            kwargs["serializer"] = serializer

        compression = compression or self.compression
        if compression:
            kwargs["compression"] = compression

        # The cache key is the name and connection args.
        # This is so that (if needed) a fresh connection can be made with
        # different serializer/compression args.
        #
        cache_key = (queue_name, serializer, compression,)
        if cache_key not in self.queue_cache:
            self.queue_cache[cache_key] = self.broker.SimpleQueue(queue_name, **kwargs)
        return self.queue_cache[cache_key]

    def post_handle_data(self):
        """This gets called after each item has been processed.
        """
        pass

    def is_paused(self):
        """Return True if the Consumer should be paused. This is checked before
        *every* handle item (and repeatedly if the consumer is paused), so if
        a custom is_pause is provided then don't make it expensive!
        """
        # A default consumer never pauses.
        #
        return False

    def run_forever(self):
        """Keep running (unless we get a Ctrl-C).
        """
        while True:
            try:
                is_running = True
                while self.is_paused():
                    if is_running:
                        logging.info("consumer is now paused")
                        is_running = False
                    # Don't move on to the next message until we are unpaused!
                    #
                    time.sleep(self.pause_delay)

                # Only log this if we came out of the while loop.
                #
                if not is_running:
                    logging.info("consumer is not paused")

                message = self.source_queue.get(block=True)
                data = message.payload

                with stats.time_block(self.statsd_client):
                    new_messages = self.handle_data(data)

                # Must be successful if we have reached here.
                stats.mark_successful_job(self.statsd_client)

                self.post_handle_data()

            except KeyboardInterrupt:
                logging.info("Caught Ctrl-C. Byee!")
                # Break out of our loop.
                #
                break

            except:
                # Keep going, but don't ack the message.
                # Also, log the exception.
                logging.exception("Exception handling data")

                if self.handle_exception is not None:
                    self.handle_exception()

                if self.requeue:
                    message.requeue()
                elif self.reject:
                    message.reject()

                stats.mark_failed_job(self.statsd_client)

            else:
                # Queue up the new messages (if any).
                #
                if new_messages:
                    for queue_name, data in new_messages:
                        destination_queue = self.get_queue(queue_name)
                        destination_queue.put(data)

                # We're done with the original message.
                #
                message.ack()

    def get_full_statsd_prefix(self, base_prefix, queuename):
        """Return a key that is unique to this worker.
        So it will be queue_name.hostname.workerid
        """
        hostname_raw = socket.gethostname()
        # We remove '.' chars from the hostname because statsd uses those to
        # group prefixes.
        hostname = hostname_raw.replace(".", "_")

        # If we have specified a workerid then use it, otherwise use the OS pid
        # (that's bound to be unique per host).
        if self.workerid is not None:
            workerid = self.workerid
        else:
            workerid = str(os.getpid())

        return "{0}.{1}.{2}.{3}".format(base_prefix, queuename, hostname, workerid)
