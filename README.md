# Immich HEIC to JPG

A cautious Immich sidecar that finds HEIC/HEIF image assets, converts them to JPG, and uploads the JPG copies back to Immich through the public API.

The app includes a web dashboard, dry-run scans, persistent conversion state, and a daily scheduler intended for overnight runs.

## Safety Model

- Originals are never deleted.
- Immich's database is never edited directly.
- Files inside Immich's managed upload storage are never modified.
- Converted JPGs are uploaded as new Immich assets.
- Dry-run mode is enabled by default.
- The dashboard never displays the Immich API key.
- State is stored in `/state` so repeated runs can skip assets already handled.

This is intentionally a sidecar rather than a native Immich plugin. The Immich 3.0.0-era docs expose API access, CLI upload, external libraries, XMP sidecars, and admin jobs, but not a first-party plugin hook for replacing originals. Immich's backup/filesystem docs also warn against manually changing files inside managed upload storage, so this tool uses only supported API calls.

Relevant Immich docs:

- [API docs](https://docs.immich.app/api/)
- [Supported media formats](https://docs.immich.app/features/supported-formats/)
- [CLI/API key docs](https://docs.immich.app/features/command-line-interface/)
- [External libraries](https://docs.immich.app/features/libraries/)
- [Backup and filesystem warning](https://docs.immich.app/administration/backup-and-restore/)
- [XMP sidecars](https://docs.immich.app/features/xmp-sidecars/)

## Dashboard

Open the dashboard at:

```text
http://SERVER-IP:8080/
```

The dashboard shows:

- current scan status
- HEIC/HEIF matches from the latest run
- planned conversions during dry runs
- successful JPG uploads
- skipped duplicates
- failed items
- next scheduled automatic scan
- recent activity
- safety checks

Buttons are available for:

- `Run Dry Scan Now`
- `Run Configured Scan Now`

`Run Configured Scan Now` follows the `DRY_RUN` environment variable. If `DRY_RUN=true`, it will still only do a dry run.

## Docker Compose

1. Copy the example environment file.

```powershell
Copy-Item .env.example .env
```

2. Edit `.env`.

```dotenv
IMMICH_API_URL=http://host.docker.internal:2283/api
IMMICH_API_KEY=replace-with-your-api-key
DRY_RUN=true
DAILY_SCAN_ENABLED=true
DAILY_SCAN_TIME=02:30
TZ=America/Denver
```

3. Start the app.

```powershell
docker compose up -d
```

4. Open the dashboard and run a dry scan.

5. When the dry scan looks right, set:

```dotenv
DRY_RUN=false
```

6. Restart the container.

```powershell
docker compose up -d
```

The app will then run once per day at `DAILY_SCAN_TIME` and can also be started manually from the dashboard.

## Unraid Install

This repository includes an Unraid template at:

```text
templates/immich-heic.xml
```

After the GitHub Container Registry image has been published by the included GitHub Actions workflow, install on Unraid using this repository as a template source.

### Template Repository URL

Use this URL in Unraid's Docker template repositories:

```text
https://github.com/UnknownIntegral/Immich-HEIC
```

### Unraid Settings

Recommended first install settings:

| Setting | Recommended value |
| --- | --- |
| `Repository` | `ghcr.io/unknownintegral/immich-heic:latest` |
| `Web UI Port` | `8080` |
| `State Storage` | `/mnt/user/appdata/immich-heic` |
| `Immich API URL` | `http://immich:2283/api` if reachable by container name, otherwise your server URL |
| `Immich API Key` | API key with asset read and asset upload permissions |
| `Dry Run` | `true` for first install |
| `Daily Scan Enabled` | `true` |
| `Daily Scan Time` | `02:30` |
| `Timezone` | Your local timezone, for example `America/Denver` |

### First Run on Unraid

1. Install the template with `Dry Run=true`.
2. Open the Web UI.
3. Click `Run Dry Scan Now`.
4. Review the dashboard activity list.
5. Confirm it only plans HEIC/HEIF files you expect.
6. Change `Dry Run=false`.
7. Apply the Unraid container update.
8. Let the scheduled overnight run handle conversions, or click `Run Configured Scan Now`.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `IMMICH_API_URL` | required | API base URL, such as `http://immich:2283/api`, `http://host.docker.internal:2283/api`, or another reachable Immich server URL. |
| `IMMICH_API_KEY` | required | Immich API key with asset read and asset upload permissions. |
| `DRY_RUN` | `true` | Logs planned work without download/convert/upload. |
| `DAILY_SCAN_ENABLED` | `true` | Runs automatically once per day. |
| `DAILY_SCAN_TIME` | `02:30` | Local 24-hour time for the automatic scan. |
| `RUN_ON_START` | `false` | Run immediately on container start. Keep false for cautious unattended operation. |
| `TZ` | image default | Container timezone used for the daily schedule. |
| `WEB_PORT` | `8080` | Dashboard port inside the container. |
| `JPEG_QUALITY` | `92` | JPEG quality passed to ImageMagick or `heif-convert`. |
| `PAGE_SIZE` | `250` | Immich search page size. Immich allows up to 1000. |
| `MAX_ASSETS` | `0` | `0` means no limit. Use `1` or `5` for small confidence runs. |
| `STATE_PATH` | `/state/state.json` | Persistent conversion tracking. |
| `TMP_DIR` | `/tmp/immich-heic-to-jpg` | Temporary download and conversion workspace. |
| `REMOTE_DUPLICATE_CHECK` | `true` | Searches Immich for the expected output filename before converting. |
| `PRESERVE_METADATA` | `true` | Uses ExifTool to copy metadata from HEIC/HEIF to JPG before upload. |

## Publishing the Image

The included workflow publishes to:

```text
ghcr.io/unknownintegral/immich-heic:latest
```

The workflow runs on pushes to `main` and can also be started manually from GitHub Actions.

For Unraid installs to pull the image, make sure the GHCR package is public or that Unraid has registry credentials with access.

## Current Limitations

- Album membership and stacks are not copied yet.
- Converted JPGs appear as separate assets.
- The app does not delete the original HEIC/HEIF assets.
- External-library users who want JPG files next to original files should use a separate filesystem workflow and trigger an Immich external-library rescan. Do not use direct filesystem modification for Immich-managed upload storage.

