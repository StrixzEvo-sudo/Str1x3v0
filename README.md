# System Cleanup Utility

Small Windows temp-cleaning utility packaged as a standalone `.exe`, with a GitHub Pages site and GitHub Releases updater.

## What It Cleans

Standard targets:

- Current user temp folder
- `AppData\LocalLow\Temp`
- `C:\Windows\Temp`
- `C:\Windows\Prefetch`

Advanced cache targets:

- DirectX shader cache
- Windows thumbnail and icon cache
- Windows error reports and crash dumps
- Delivery Optimization cache
- Edge, Chrome, Brave, and Firefox cache folders
- Microsoft Store app temp and web cache folders
- Windows Update download cache

## What Is Included

- `app.py`: the Windows GUI utility
- `build.ps1`: local PyInstaller build script
- `release_config.json`: GitHub repo settings used by the built-in updater
- `docs/`: static GitHub Pages site

## Safety Notes

- It removes the contents of those folders and keeps the folders themselves.
- Files that are currently in use may be skipped.
- Reparse points and symlinks are skipped on purpose so cleanup stays inside the selected folders.
- `Prefetch` is rebuilt by Windows over time after cleanup.
- Browser and app caches are best cleaned while those apps are closed.
- Some advanced caches are rebuilt by Windows or apps after cleanup.

## Local Build

From PowerShell in this folder:

```powershell
.\build.ps1
```

The built executable is:

```text
dist\SystemCleanupUtility.exe
```

## Enable Auto-Update

Edit `release_config.json` before publishing:

```json
{
  "github_owner": "StrixzEvo-sudo",
  "github_repo": "Str1x3v0",
  "asset_name": "SystemCleanupUtility.exe"
}
```

The updater is embedded into the built `.exe`, so end users only need the executable.

## Publish For Anyone

1. Create a GitHub repo and push this project to the `main` branch.
2. Set `release_config.json` to your real GitHub owner and repo.
3. In the repository settings, enable GitHub Pages from the `main` branch and `/docs` folder.
4. Create a GitHub Release and upload `dist/SystemCleanupUtility.exe`.

Example tag:

```powershell
git tag v0.1.0
git push origin main --tags
```

After that:

- the site can be shared from GitHub Pages
- the download button points to the latest `.exe`
- the app checks GitHub Releases on launch and offers to install new versions

## Versioning

When you ship a new version:

1. Update `APP_VERSION` in `app.py`
2. Commit the changes
3. Build a fresh `dist\SystemCleanupUtility.exe`
4. Create a GitHub Release like `v0.1.1` and upload the new `.exe`
