const assetName = "SystemCleanupUtility.exe";

function deriveRepoFromPagesUrl() {
  const host = window.location.hostname.toLowerCase();
  if (!host.endsWith(".github.io")) {
    return null;
  }

  const owner = host.split(".")[0];
  const pathParts = window.location.pathname.split("/").filter(Boolean);
  const repo = pathParts[0];
  if (!owner || !repo) {
    return null;
  }

  return { owner, repo };
}

function setButtonState(element, enabled, href = "#") {
  element.href = href;
  element.setAttribute("aria-disabled", String(!enabled));
  element.classList.toggle("disabled", !enabled);
}

function setStatus(message, tone) {
  const status = document.getElementById("release-status");
  status.textContent = message;
  status.dataset.tone = tone;
}

function formatSize(bytes) {
  if (!bytes || Number.isNaN(bytes)) {
    return "Unknown";
  }

  let value = bytes;
  const units = ["B", "KB", "MB", "GB"];
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function firstUsefulLine(text) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
}

async function loadReleaseInfo() {
  const repo = deriveRepoFromPagesUrl();
  const downloadLink = document.getElementById("download-link");
  const releaseLink = document.getElementById("release-link");
  const repoSlug = document.getElementById("repo-slug");
  const releaseVersion = document.getElementById("release-version");
  const releaseUpdated = document.getElementById("release-updated");
  const releaseSize = document.getElementById("release-size");
  const releaseNote = document.getElementById("release-note");

  if (!repo) {
    setStatus("Publish this folder with GitHub Pages to activate the live download button.", "warn");
    return;
  }

  const slug = `${repo.owner}/${repo.repo}`;
  const releasesPage = `https://github.com/${slug}/releases`;
  const latestDownload = `https://github.com/${slug}/releases/latest/download/${assetName}`;
  const apiUrl = `https://api.github.com/repos/${slug}/releases/latest`;

  repoSlug.textContent = slug;
  setButtonState(releaseLink, true, releasesPage);

  try {
    const response = await fetch(apiUrl, {
      headers: {
        Accept: "application/vnd.github+json"
      }
    });

    if (!response.ok) {
      throw new Error(`GitHub API returned ${response.status}`);
    }

    const release = await response.json();
    const asset = release.assets.find((item) => item.name === assetName)
      || release.assets.find((item) => item.name.toLowerCase().endsWith(".exe"));

    releaseVersion.textContent = release.tag_name || "Latest release";
    releaseUpdated.textContent = release.published_at
      ? new Date(release.published_at).toLocaleDateString(undefined, {
          year: "numeric",
          month: "short",
          day: "numeric"
        })
      : "Unknown";
    releaseSize.textContent = asset ? formatSize(asset.size) : "Unknown";
    releaseNote.textContent = firstUsefulLine(release.body || "")
      || "Latest FreshStart build is ready to download.";

    setButtonState(downloadLink, true, latestDownload);
    setStatus(`Live now: ${release.tag_name || "latest build"} is ready to download.`, "ok");
  } catch (error) {
    releaseVersion.textContent = "No release yet";
    releaseUpdated.textContent = "Publish a release";
    releaseSize.textContent = "Unavailable";
    releaseNote.textContent = "The page is live. Publish a GitHub release to activate the full download experience.";
    setStatus("The page is live. Publish a release to activate the download button.", "warn");
  }
}

document.addEventListener("DOMContentLoaded", loadReleaseInfo);
