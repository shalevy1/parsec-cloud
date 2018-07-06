import trio
import pickle

try:
    from parsec.utils import sym_encrypt, sign
except ImportError:

    def sym_encrypt(key, content):
        return content

    def sign(key, content):
        return content


from parsec.core.fs.data import (
    is_file_manifest,
    is_folder_manifest,
    remote_to_local_manifest,
    local_to_remote_manifest,
)
from parsec.core.fs.local_folder_fs import FSManifestLocalMiss
from parsec.core.local_db import LocalDBMissingEntry
from parsec.utils import to_jsonb64


class Syncer:
    def __init__(self, device, backend_conn, local_manifest_fs, signal_ns):
        self._lock = trio.Lock()
        self.device = device
        self._backend_conn = backend_conn
        self._local_manifest_fs = local_manifest_fs
        self.signal_ns = signal_ns

    def _build_beacon_messages(self, notify, msg):
        raw_msg = pickle.dumps(msg)
        signed_msg = sign(self.device.device_signkey, raw_msg)
        notify_msgs = []
        for beacon_id, beacon_key in notify:
            beacon_ciphered_msg = sym_encrypt(beacon_key, signed_msg)
            notify_msgs.append((beacon_id, beacon_ciphered_msg))
        return notify_msgs

    def _get_group_check_local_entries(self):
        entries = []

        def _recursive_get_local_entries_ids(access):
            try:
                manifest = self._local_manifest_fs.get_manifest(access)
            except LocalDBMissingEntry:
                return

            if is_folder_manifest(manifest):
                for child_access in manifest["children"].values():
                    _recursive_get_local_entries_ids(child_access)
            print(access)

            entries.append(
                {"id": access["id"], "rts": access["rts"], "version": manifest["base_version"]}
            )

        _recursive_get_local_entries_ids(self._local_manifest_fs.root_access)
        return entries

    async def full_sync(self):
        local_entries = self._get_group_check_local_entries()
        need_sync_entries = await self._backend_vlob_group_check(local_entries)
        for chaned_item in need_sync_entries["changed"]:
            await self.sync_by_id(chaned_item["id"])

    async def sync_by_id(self, entry_id):
        try:
            path, access, _ = self._local_manifest_fs.get_entry_path(entry_id)
            notify = self._local_manifest_fs.get_beacons(path)
        except FSManifestLocalMiss:
            # Entry not locally present, nothing to do
            return
        await self.sync(access, notify=notify)

    async def sync(self, access, recursive=True, notify=()):
        # Only allow a single synchronizing operation at a time to simplify
        # concurrency. Beside concurrent syncs would make each sync operation
        # slower which would make them less reliable with poor backend connection.
        async with self._lock:
            await self._sync_nolock(access, recursive, notify)

    async def _backend_block_post(self, access, blob):
        payload = {"cmd": "blockstore_post", "id": access["id"], "block": to_jsonb64(blob)}
        ret = await self._backend_conn.send(payload)
        assert ret["status"] == "ok"
        return ret

    async def _backend_vlob_group_check(self, to_check):
        payload = {"cmd": "vlob_group_check", "to_check": to_check}
        ret = await self._backend_conn.send(payload)
        assert ret["status"] == "ok"
        return ret

    async def _backend_vlob_create(self, id, rts, wts, blob, notify):
        payload = {"cmd": "vlob_create", "id": id, "wts": wts, "rts": rts, "blob": to_jsonb64(blob)}
        ret = await self._backend_conn.send(payload)
        assert ret["status"] == "ok"
        return ret

    async def _backend_vlob_update(self, id, wts, version, blob, notify):
        payload = {
            "cmd": "vlob_update",
            "id": id,
            "wts": wts,
            "version": version,
            "blob": to_jsonb64(blob),
        }
        ret = await self._backend_conn.send(payload)
        assert ret["status"] == "ok"
        return ret

    async def _sync_nolock(self, access, recursive, notify):
        msg = {"id": access["id"]}
        notify_msgs = self._build_beacon_messages(notify, msg)

        try:
            manifest = self._local_manifest_fs.get_manifest(access)
        except LocalDBMissingEntry:
            # Nothing to do if entry is no present locally
            return

        # Do complex stuff here...
        if is_file_manifest(manifest):
            if not manifest["need_sync"]:
                self._local_manifest_fs.mark_outdated_manifest(access)
                self.signal_ns.signal("fs.entry.synced").send("local", id=access["id"])
                return

            for db_access in manifest["dirty_blocks"]:
                db = self._local_manifest_fs.get_manifest(db_access)
                await self._backend_block_post(db_access, db)
            manifest["blocks"] += manifest["dirty_blocks"]

            remote_manifest = {
                "type": "file_manifest",
                "version": manifest["base_version"] + 1,
                "blocks": manifest["blocks"] + manifest["dirty_blocks"],
                "created": manifest["created"],
                "updated": manifest["updated"],
                "size": manifest["size"],
                "author": self.device.id,
            }

            raw = pickle.dumps(remote_manifest)
            signed = sign(self.device.device_signkey, raw)
            ciphered = sym_encrypt(access["key"], signed)
            if manifest["is_placeholder"]:
                await self._backend_vlob_create(
                    access["id"], access["rts"], access["wts"], ciphered, notify=notify_msgs
                )
            else:
                await self._backend_vlob_update(
                    access["id"],
                    access["wts"],
                    remote_manifest["version"],
                    ciphered,
                    notify=notify_msgs,
                )

            # Fuck the merge...
            updated_manifest = remote_to_local_manifest(remote_manifest)
            self._local_manifest_fs.set_manifest(access, updated_manifest)

        else:
            if recursive:
                for child_name, child_access in manifest["children"].items():
                    if isinstance(recursive, dict):
                        child_recursive = recursive.get(child_name, False)
                    else:
                        child_recursive = recursive
                    await self._sync_nolock(child_access, recursive=child_recursive, notify=notify)

            # If recursive=False, placeholder are stored in parent but not resolved...

            if not manifest["need_sync"]:
                self._local_manifest_fs.mark_outdated_manifest(access)
                self.signal_ns.signal("fs.entry.synced").send("local", id=access["id"])
                return

            remote_manifest = local_to_remote_manifest(manifest)
            remote_manifest["version"] += 1

            raw = pickle.dumps(remote_manifest)
            signed = sign(self.device.device_signkey, raw)
            ciphered = sym_encrypt(access["key"], signed)
            if manifest["is_placeholder"]:
                await self._backend_vlob_create(
                    access["id"], access["rts"], access["wts"], ciphered, notify=notify_msgs
                )
            else:
                await self._backend_vlob_update(
                    access["id"],
                    access["wts"],
                    remote_manifest["version"],
                    ciphered,
                    notify=notify_msgs,
                )

            # Fuck the merge...
            updated_manifest = remote_to_local_manifest(remote_manifest)
            self._local_manifest_fs.set_manifest(access, updated_manifest)

        self.signal_ns.signal("fs.entry.synced").send("local", id=access["id"])