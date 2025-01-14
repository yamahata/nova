# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright 2011 Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


from nova.utils import import_object
from nova.rpc.common import RemoteError, LOG
from nova import flags

FLAGS = flags.FLAGS
flags.DEFINE_string('rpc_backend',
                    'nova.rpc.impl_kombu',
                    "The messaging module to use, defaults to kombu.")


def create_connection(new=True):
    """Create a connection to the message bus used for rpc.

    For some example usage of creating a connection and some consumers on that
    connection, see nova.service.

    :param new: Whether or not to create a new connection.  A new connection
                will be created by default.  If new is False, the
                implementation is free to return an existing connection from a
                pool.

    :returns: An instance of nova.rpc.common.Connection
    """
    return _get_impl().create_connection(new=new)


def call(context, topic, msg):
    """Invoke a remote method that returns something.

    :param context: Information that identifies the user that has made this
                    request.
    :param topic: The topic to send the rpc message to.  This correlates to the
                  topic argument of
                  nova.rpc.common.Connection.create_consumer() and only applies
                  when the consumer was created with fanout=False.
    :param msg: This is a dict in the form { "method" : "method_to_invoke",
                                             "args" : dict_of_kwargs }

    :returns: A dict from the remote method.
    """
    return _get_impl().call(context, topic, msg)


def cast(context, topic, msg):
    """Invoke a remote method that does not return anything.

    :param context: Information that identifies the user that has made this
                    request.
    :param topic: The topic to send the rpc message to.  This correlates to the
                  topic argument of
                  nova.rpc.common.Connection.create_consumer() and only applies
                  when the consumer was created with fanout=False.
    :param msg: This is a dict in the form { "method" : "method_to_invoke",
                                             "args" : dict_of_kwargs }

    :returns: None
    """
    return _get_impl().cast(context, topic, msg)


def fanout_cast(context, topic, msg):
    """Broadcast a remote method invocation with no return.

    This method will get invoked on all consumers that were set up with this
    topic name and fanout=True.

    :param context: Information that identifies the user that has made this
                    request.
    :param topic: The topic to send the rpc message to.  This correlates to the
                  topic argument of
                  nova.rpc.common.Connection.create_consumer() and only applies
                  when the consumer was created with fanout=True.
    :param msg: This is a dict in the form { "method" : "method_to_invoke",
                                             "args" : dict_of_kwargs }

    :returns: None
    """
    return _get_impl().fanout_cast(context, topic, msg)


def multicall(context, topic, msg):
    """Invoke a remote method and get back an iterator.

    In this case, the remote method will be returning multiple values in
    separate messages, so the return values can be processed as the come in via
    an iterator.

    :param context: Information that identifies the user that has made this
                    request.
    :param topic: The topic to send the rpc message to.  This correlates to the
                  topic argument of
                  nova.rpc.common.Connection.create_consumer() and only applies
                  when the consumer was created with fanout=False.
    :param msg: This is a dict in the form { "method" : "method_to_invoke",
                                             "args" : dict_of_kwargs }

    :returns: An iterator.  The iterator will yield a tuple (N, X) where N is
              an index that starts at 0 and increases by one for each value
              returned and X is the Nth value that was returned by the remote
              method.
    """
    return _get_impl().multicall(context, topic, msg)


_RPCIMPL = None


def _get_impl():
    """Delay import of rpc_backend until FLAGS are loaded."""
    global _RPCIMPL
    if _RPCIMPL is None:
        _RPCIMPL = import_object(FLAGS.rpc_backend)
    return _RPCIMPL
