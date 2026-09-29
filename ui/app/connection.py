from __future__ import annotations

from datetime import timedelta
from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions


def create_cluster(settings):
    """Create one portable SDK connection for Server or Capella.

    couchbases:// enables TLS. The WAN profile is intended for developer
    workstations connecting across regions; production apps should normally run
    close to the cluster with CB_WAN_PROFILE=false.
    """
    auth = PasswordAuthenticator(settings.cb_username, settings.cb_password)
    options = ClusterOptions(auth)
    if settings.cb_wan_profile:
        options.apply_profile('wan_development')
    cluster = Cluster.connect(settings.cb_conn_string, options)
    cluster.wait_until_ready(timedelta(seconds=settings.cb_connect_timeout_seconds))
    return cluster
