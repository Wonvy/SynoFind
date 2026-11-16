# Suite implementation contract

- The DSM suite uses the documented `SYNO.API.Auth`, `SYNO.FileStation.Search`, `SYNO.FileStation.List`, and `SYNO.FileStation.Download` APIs.
- Search is by file name and metadata only. It never walks the NAS filesystem or publishes a local file index.
- DSM search tasks are started, polled, paginated, then stopped when needed and cleaned up.
- DSM credentials are read from the private suite config and excluded from settings responses. The config file is mode `0600` inside the package data directory.
- File IDs are authenticated, scoped to the selected File Station path, and revalidated with DSM before metadata or content is returned.
- `GET /index/status` reports connection state only. There is no scan endpoint or scheduled scan.
- `GET /settings` returns `{root_path,smb_root,dsm_url,dsm_username,has_dsm_password,port}`. `PUT /settings` tests account login and read permission for the selected File Station path before saving.
- `synofind init --data-dir PATH --port 8787 --root /Wonvy --smb-root \\\\nas\\Wonvy` reads the initial SynoFind web password from standard input. DSM credentials are configured in the web settings and are not passed in argv.
