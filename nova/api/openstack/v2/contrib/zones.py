# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 OpenStack LLC.
# All Rights Reserved.
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

"""The zones extension."""


from nova import flags
from nova import log as logging
from nova.api.openstack.v2 import extensions


LOG = logging.getLogger("nova.api.openstack.v2.contrib.zones")
FLAGS = flags.FLAGS


class Zones(extensions.ExtensionDescriptor):
    """Enables zones-related functionality such as adding child zones,
    listing child zones, getting the capabilities of the local zone,
    and returning build plans to parent zones' schedulers
    """

    name = "Zones"
    alias = "os-zones"
    namespace = "http://docs.openstack.org/compute/ext/zones/api/v1.1"
    updated = "2011-09-21T00:00:00+00:00"

    def get_resources(self):
        # Nothing yet.
        return []
