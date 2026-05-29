def classFactory(iface):
    from .qd_rip_plugin import QDRIPPlugin
    return QDRIPPlugin(iface)
