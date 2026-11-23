"""iMou (Imou Life) cameras through the official iMou Open Platform API ("route A"), watch only.

Pure Python, no Qt. Nothing here imports another camera vendor's module, and the client can only build the
few requests listed in :data:`tapo_desktop.core.imou.client.ALLOWED_PATHS` (token, device list, stream URL, live
bind / query / delete). It has no call that changes a camera or the account's device list.
"""
