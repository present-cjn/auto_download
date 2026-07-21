# Bundled rclone

Place the Windows `rclone.exe` binary in this directory before building the
local Windows app package:

```text
vendor/rclone/rclone.exe
```

The application uses `RCLONE_BIN` when it is set. If `RCLONE_BIN` is not set,
it first looks for this bundled binary, then falls back to `rclone` on `PATH`.

Do not commit personal `rclone.conf` files or Google OAuth tokens.
