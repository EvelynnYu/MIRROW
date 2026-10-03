#!/usr/bin/env node
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const statePath = path.join(rootDir, ".listening-state.json");
let webPlayerServer = null;
let webPlayerPort = null;
let playbackOperation = Promise.resolve();
let pendingPlaybackState = null;
let pendingPlaybackExpiresAt = 0;
let replayRequestGuard = null;
let activeWebClientToken = null;
let activeWebClientSeenAt = 0;

const appData = process.env.APPDATA ?? "";
const neteaseCliScript = path.join(appData, "npm", "node_modules", "neteasecli", "dist", "index.js");
const neteaseCliClientModule = path.join(appData, "npm", "node_modules", "neteasecli", "dist", "api", "client.js");
const neteaseCliTrackModule = path.join(appData, "npm", "node_modules", "neteasecli", "dist", "api", "track.js");

const runtimeEnv = {
  ...process.env,
  PATH: `${rootDir}${path.delimiter}${process.env.PATH ?? ""}`,
};

function textResult(data) {
  return {
    content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
    structuredContent: data,
  };
}

function jsonResponse(res, statusCode, data) {
  const body = JSON.stringify(data);
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}

function htmlResponse(res, html) {
  res.writeHead(200, {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(html);
}

async function readRequestJson(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  return text ? JSON.parse(text) : {};
}

function failResult(message, extra = {}) {
  return textResult({ success: false, message, ...extra });
}

async function commandExists(command) {
  const ps = `Get-Command ${command} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source`;
  try {
    const { stdout } = await execFileAsync("powershell.exe", ["-NoProfile", "-Command", ps], {
      timeout: 5000,
      windowsHide: true,
    });
    return stdout.trim();
  } catch {
    return "";
  }
}

async function findMpv() {
  const fromPath = await commandExists("mpv");
  if (fromPath) {
    return { available: true, path: fromPath, source: "PATH" };
  }

  const localMpv = process.platform === "win32" ? path.join(rootDir, "mpv.exe") : path.join(rootDir, "mpv");
  if (await exists(localMpv)) {
    return { available: true, path: localMpv, source: "project-local" };
  }

  return { available: false, path: "", source: "missing" };
}

async function runNodeScript(scriptPath, args, { timeout = 30000 } = {}) {
  const { stdout, stderr } = await execFileAsync(process.execPath, [scriptPath, ...args], {
    cwd: rootDir,
    env: runtimeEnv,
    timeout,
    windowsHide: true,
    maxBuffer: 8 * 1024 * 1024,
  });
  return { stdout: stdout.trim(), stderr: stderr.trim() };
}

async function runNetease(args, options) {
  return runNodeScript(neteaseCliScript, args, options);
}

function parseJson(stdout, label) {
  if (!stdout) {
    return {};
  }
  try {
    return JSON.parse(stdout);
  } catch (error) {
    throw new Error(`${label} returned non-JSON output: ${stdout.slice(0, 500)}`);
  }
}

async function readState() {
  try {
    return JSON.parse(await fs.readFile(statePath, "utf8"));
  } catch {
    return null;
  }
}

async function writeState(state) {
  await fs.writeFile(statePath, JSON.stringify(state, null, 2), "utf8");
}

async function clearState() {
  try {
    await fs.unlink(statePath);
  } catch {
    // No state yet.
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function stopMpvProcesses() {
  if (process.platform !== "win32") {
    try {
      const { stdout } = await execFileAsync("pkill", ["-f", "mpv"], {
        timeout: 5000,
      });
      return { success: true, stdout: stdout.trim() };
    } catch (error) {
      return { success: false, message: String(error.message ?? error) };
    }
  }
  const ps = [
    "$targets = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in @('mpv','mpv.com') }",
    "$ids = @($targets | Select-Object -ExpandProperty Id)",
    "$targets | Stop-Process -Force -ErrorAction SilentlyContinue",
    "$ids -join ','",
  ].join("; ");
  try {
    const { stdout } = await execFileAsync("powershell.exe", ["-NoProfile", "-Command", ps], {
      timeout: 5000,
      windowsHide: true,
    });
    const stoppedIds = stdout.trim().split(",").map((id) => id.trim()).filter(Boolean);
    return { success: true, stoppedIds };
  } catch (error) {
    return { success: false, message: String(error.message ?? error) };
  }
}

async function stopPlaybackBestEffort() {
  clearPendingPlaybackState();
  clearReplayRequestGuard();
  let playerStop = null;
  try {
    playerStop = parseJson((await runNetease(["--pretty", "player", "stop"])).stdout, "neteasecli player stop");
  } catch (error) {
    playerStop = { success: false, message: String(error.message ?? error) };
  }
  const mpvStop = await stopMpvProcesses();
  await clearState();
  return { playerStop, mpvStop };
}

async function stopActivePlaybackBestEffort() {
  let playerStop = null;
  try {
    playerStop = parseJson((await runNetease(["--pretty", "player", "stop"])).stdout, "neteasecli player stop");
  } catch (error) {
    playerStop = { success: false, message: String(error.message ?? error) };
  }
  const mpvStop = await stopMpvProcesses();
  return { playerStop, mpvStop };
}

async function withPlaybackLock(action) {
  const previous = playbackOperation.catch(() => {});
  let release;
  playbackOperation = new Promise((resolve) => {
    release = resolve;
  });
  await previous;
  try {
    return await action();
  } finally {
    release();
  }
}

async function closeWebPlayerBestEffort() {
  const fallbackPort = webPlayerPort || 8765;
  if (!webPlayerServer) {
    const standalone = await closeStandaloneWebPlayerBestEffort(fallbackPort);
    return standalone.closed
      ? standalone
      : { success: true, closed: false, message: "Web player was not running.", standalone };
  }
  const serverToClose = webPlayerServer;
  webPlayerServer = null;
  webPlayerPort = null;
  try {
    await new Promise((resolve, reject) => {
      serverToClose.close((error) => error ? reject(error) : resolve());
    });
    return { success: true, closed: true };
  } catch (error) {
    return { success: false, closed: false, message: String(error.message ?? error) };
  }
}

async function closeStandaloneWebPlayerBestEffort(port = 8765) {
  if (process.platform !== "win32") {
    return { success: true, closed: false, message: "Standalone web player cleanup is only implemented for Windows." };
  }
  const scriptPath = path.join("src", "server.js");
  const ps = [
    `$port = ${Number(port) || 8765}`,
    `$root = ${psQuote(rootDir)}`,
    `$script = ${psQuote(scriptPath)}`,
    "$listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue)",
    "$stopped = @()",
    "foreach ($listener in $listeners) {",
    "  $ownerPid = [int]$listener.OwningProcess",
    "  if ($ownerPid -eq $PID) { continue }",
    "  $process = Get-CimInstance Win32_Process -Filter \"ProcessId = $ownerPid\" -ErrorAction SilentlyContinue",
    "  $cmd = [string]$process.CommandLine",
    "  $isThisProject = $cmd.Contains($root) -or $cmd.Contains($script)",
    "  $isWebPlayer = $cmd.Contains('--web-player')",
    "  if ($isThisProject -and $isWebPlayer) {",
    "    Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue",
    "    $stopped += $ownerPid",
    "  }",
    "}",
    "$stopped -join ','",
  ].join("; ");
  try {
    const { stdout } = await execFileAsync("powershell.exe", ["-NoProfile", "-Command", ps], {
      timeout: 5000,
      windowsHide: true,
    });
    const stoppedIds = stdout.trim().split(",").map((id) => id.trim()).filter(Boolean);
    return {
      success: true,
      closed: stoppedIds.length > 0,
      port,
      stoppedIds,
      message: stoppedIds.length ? "Closed standalone web player process." : "No standalone web player process found.",
    };
  } catch (error) {
    return { success: false, closed: false, port, message: String(error.message ?? error) };
  }
}

function normalizeArtists(track) {
  const artists = track?.artists ?? track?.ar ?? track?.fullArtists ?? [];
  return artists.map((artist) => artist.name).filter(Boolean).join(" / ");
}

function normalizeTrack(track) {
  return {
    id: String(track.originalId ?? track.id),
    encryptedId: track.id && /^[0-9A-F]{32}$/i.test(String(track.id)) ? String(track.id) : undefined,
    name: track.name,
    artist: normalizeArtists(track),
    album: track.album?.name ?? track.al?.name ?? "",
    durationMs: track.duration ?? track.dt ?? null,
    raw: track,
  };
}

function mainTitleText(value) {
  if (!value || typeof value !== "object") return "";
  const mainTitle = value.mainTitle;
  if (!mainTitle || typeof mainTitle !== "object") return "";
  return String(mainTitle.title ?? "").trim();
}

function textLinkTitles(value) {
  if (!value || typeof value !== "object" || !Array.isArray(value.textLinks)) return [];
  const titles = [];
  for (const item of value.textLinks) {
    const title = String(item?.text ?? "").trim();
    if (title && !titles.includes(title)) titles.push(title);
  }
  return titles;
}

function resourceTitles(resources) {
  if (!Array.isArray(resources)) return [];
  const titles = [];
  for (const item of resources) {
    const title = mainTitleText(item?.uiElement);
    if (title && !titles.includes(title)) titles.push(title);
  }
  return titles;
}

function extractTrackWikiMetadata(payload) {
  const data = payload?.data && typeof payload.data === "object" ? payload.data : payload;
  const blocks = Array.isArray(data?.blocks) ? data.blocks : [];
  const genres = [];
  const recommendedTags = [];
  const languages = [];

  for (const block of blocks) {
    if (!block || typeof block !== "object" || block.showType !== "SONG_PLAY_ABOUT_TAB_SONG_BASIC") {
      continue;
    }
    const creatives = Array.isArray(block.creatives) ? block.creatives : [];
    for (const creative of creatives) {
      if (!creative || typeof creative !== "object") continue;
      const creativeType = String(creative.creativeType ?? "").trim();
      const title = mainTitleText(creative.uiElement);
      const titles = resourceTitles(creative.resources);
      if (creativeType === "songTag" || title === "曲风") {
        for (const item of titles) {
          if (!genres.includes(item)) genres.push(item);
        }
      } else if (creativeType === "songBizTag" || title === "推荐标签") {
        for (const item of titles) {
          if (!recommendedTags.includes(item)) recommendedTags.push(item);
        }
      } else if (creativeType === "language" || title === "语种") {
        for (const item of textLinkTitles(creative.uiElement)) {
          if (!languages.includes(item)) languages.push(item);
        }
      }
    }
  }

  return {
    genres,
    genre: genres.join("、"),
    recommended_tags: recommendedTags,
    language: languages.join("、"),
  };
}

async function getTrackWikiMetadata(id) {
  if (!await exists(neteaseCliClientModule)) {
    return { id: String(id), source: "netease", genres: [], genre: "", recommended_tags: [], language: "" };
  }
  const module = await import(pathToFileURL(neteaseCliClientModule).href);
  const client = module.getApiClient();
  const payload = await client.request("/song/play/about/block/page", { songId: String(id) });
  return {
    id: String(id),
    source: "netease_song_wiki",
    ...extractTrackWikiMetadata(payload),
  };
}

async function searchTracks(keyword, limit = 10) {
  const errors = [];

  if (await exists(neteaseCliClientModule)) {
    try {
      const module = await import(pathToFileURL(neteaseCliClientModule).href);
      const client = module.getApiClient();
      const payload = await client.request("/search/get", {
        s: keyword,
        type: 1,
        limit,
        offset: 0,
      });
      const records = searchRecordsFromJson(payload);
      if (records.length > 0) {
        return records.map(normalizeTrack);
      }
      errors.push("netease API client returned no songs");
    } catch (error) {
      errors.push(`netease API client failed: ${String(error.message ?? error)}`);
    }
  }

  try {
    const { stdout } = await runNetease(["--pretty", "search", "track", keyword, "--limit", String(limit)]);
    const json = parseJson(stdout, "neteasecli search");
    if (json.success === false) {
      throw new Error(json.error?.message ?? json.error ?? "neteasecli search failed");
    }
    return searchRecordsFromJson(json).map(normalizeTrack);
  } catch (error) {
    errors.push(`neteasecli command failed: ${String(error.message ?? error)}`);
  }

  throw new Error(`Could not search NetEase songs for "${keyword}". ${errors.join(" | ")}`);
}

function searchRecordsFromJson(json) {
  const candidates = [
    json?.data?.tracks,
    json?.data?.records,
    json?.data?.songs,
    json?.result?.songs,
    json?.tracks,
    json?.songs,
  ];
  return candidates.find(Array.isArray) ?? [];
}

async function getTrackDetail(id) {
  const { stdout } = await runNetease(["--pretty", "track", "detail", String(id)]);
  const json = parseJson(stdout, "neteasecli track detail");
  if (!json.success) {
    throw new Error(json.error?.message ?? json.error ?? `Could not get track detail for ${id}`);
  }
  const data = json.data;
  return {
    id: String(data.id),
    name: data.name,
    artist: (data.artists ?? []).map((artist) => artist.name).join(" / "),
    album: data.album?.name ?? "",
    albumPicUrl: data.album?.picUrl ?? "",
    durationMs: data.duration ?? null,
  };
}

async function getLyrics(id) {
  const { stdout } = await runNetease(["--pretty", "track", "lyric", String(id)]);
  const json = parseJson(stdout, "neteasecli track lyric");
  if (!json.success) {
    return { lrc: "", lines: [] };
  }
  const lrc = extractLrcText(json.data?.lrc);
  const translatedLrc = [
    json.data?.tlyric,
    json.data?.translatedLyric,
    json.data?.translated_lrc,
    json.data?.translation,
    json.data?.tlrc,
  ].map(extractLrcText).find(Boolean) ?? "";
  return {
    lrc,
    translatedLrc,
    lines: mergeTranslatedLyrics(parseLrc(lrc), parseLrc(translatedLrc)),
  };
}

async function getUserPlaylists() {
  const { stdout } = await runNetease(["--pretty", "playlist", "list"], { timeout: 30000 });
  const json = parseJson(stdout, "neteasecli playlist list");
  if (!json.success) {
    throw new Error(json.error?.message ?? json.error ?? "Could not load playlists");
  }
  return json.data?.playlists ?? [];
}

async function getPlaylistDetail(id, limit = 8) {
  const { stdout } = await runNetease(["--pretty", "playlist", "detail", String(id), "--limit", String(limit)], {
    timeout: 30000,
  });
  const json = parseJson(stdout, "neteasecli playlist detail");
  if (!json.success) {
    throw new Error(json.error?.message ?? json.error ?? `Could not load playlist ${id}`);
  }
  return json.data;
}

async function getLikedTracks(limit = 50) {
  const { stdout } = await runNetease(["--pretty", "library", "liked", "--limit", String(limit)], {
    timeout: 30000,
  });
  const json = parseJson(stdout, "neteasecli library liked");
  if (!json.success) {
    throw new Error(json.error?.message ?? json.error ?? "Could not load liked tracks");
  }
  return {
    tracks: json.data?.tracks ?? [],
    total: json.data?.total ?? 0,
  };
}

async function enrichTracks(tracks, limit = 30) {
  if (!await exists(neteaseCliTrackModule)) return tracks;
  const ids = tracks.slice(0, limit).map((track) => track.id).filter(Boolean);
  if (ids.length === 0) return tracks;
  try {
    const module = await import(pathToFileURL(neteaseCliTrackModule).href);
    const details = await module.getTrackDetails(ids);
    const detailById = new Map(details.map((detail) => [String(detail.id), detail]));
    return tracks.map((track) => {
      const detail = detailById.get(String(track.id));
      if (!detail) return track;
      return {
        ...track,
        artist: track.artist || detail.artists?.map((artist) => artist.name).join(", ") || "",
        album: track.album || detail.album?.name || "",
        duration: detail.duration ?? track.duration,
        coverUrl: detail.album?.picUrl ?? track.coverUrl ?? "",
      };
    });
  } catch {
    return tracks;
  }
}

function normalizePlaylistCard(playlist, detail = null) {
  const coverUrl = [
    detail?.coverUrl,
    detail?.coverImgUrl,
    detail?.picUrl,
    detail?.cover,
    playlist.coverUrl,
    playlist.coverImgUrl,
    playlist.picUrl,
    playlist.cover,
  ].find(Boolean) ?? "";
  return {
    id: String(playlist.id),
    name: playlist.name,
    creator: typeof playlist.creator === "string" ? playlist.creator : playlist.creator?.name ?? playlist.creator?.nickname ?? "",
    trackCount: playlist.trackCount ?? detail?.trackCount ?? 0,
    coverUrl,
  };
}

async function getPlaylistPageData(offset = 0, limit = 24) {
  const playlists = await getUserPlaylists();
  const start = Math.max(0, Number(offset || 0));
  const count = Math.max(1, Math.min(50, Number(limit || 24)));
  const page = playlists.slice(start, start + count);
  const details = await Promise.all(page.map((playlist) => (
    getPlaylistDetail(playlist.id, 1).catch(() => null)
  )));
  return {
    success: true,
    offset: start,
    limit: count,
    total: playlists.length,
    hasMore: start + page.length < playlists.length,
    playlists: page.map((playlist, index) => normalizePlaylistCard(playlist, details[index])),
  };
}

async function getRecommendedPlaylistPageData(offset = 0, limit = 14) {
  const playlists = await getUserPlaylists();
  const creator = playlists[0]?.creator ?? "";
  const recommended = playlists.filter((playlist) => playlist.creator && playlist.creator !== creator);
  const start = Math.max(0, Number(offset || 0));
  const count = Math.max(1, Math.min(50, Number(limit || 14)));
  const page = recommended.slice(start, start + count);
  const details = await Promise.all(page.map((playlist) => (
    getPlaylistDetail(playlist.id, 1).catch(() => null)
  )));
  return {
    success: true,
    offset: start,
    limit: count,
    total: recommended.length,
    hasMore: start + page.length < recommended.length,
    playlists: page.map((playlist, index) => normalizePlaylistCard(playlist, details[index])),
  };
}

async function getLikedTracksPageData(offset = 0, limit = 24) {
  const start = Math.max(0, Number(offset || 0));
  const count = Math.max(1, Math.min(50, Number(limit || 24)));
  const liked = await getLikedTracks(start + count);
  const page = (liked.tracks ?? []).slice(start, start + count);
  return {
    success: true,
    offset: start,
    limit: count,
    total: liked.total,
    hasMore: start + page.length < liked.total,
    tracks: await enrichTracks(page, count),
  };
}

async function getDashboardData() {
  const [playlists, liked] = await Promise.all([
    getUserPlaylists(),
    getLikedTracks(36),
  ]);
  const topPlaylists = playlists.slice(0, 24);
  const details = await Promise.all(topPlaylists.map((playlist) => (
    getPlaylistDetail(playlist.id, 1).catch(() => null)
  )));
  const creator = topPlaylists[0]?.creator ?? "";
  const recommendedSource = playlists
    .filter((playlist) => playlist.creator && playlist.creator !== creator)
    .slice(0, 12);
  const recommendedDetails = await Promise.all(recommendedSource.map((playlist) => (
    getPlaylistDetail(playlist.id, 1).catch(() => null)
  )));
  const recommended = recommendedSource.map((playlist, index) => normalizePlaylistCard(playlist, recommendedDetails[index]));
  return {
    success: true,
    playlists: topPlaylists.map((playlist, index) => normalizePlaylistCard(playlist, details[index])),
    playlist_total: playlists.length,
    playlist_has_more: topPlaylists.length < playlists.length,
    recommended_playlists: recommended,
    recommended_total: playlists.filter((playlist) => playlist.creator && playlist.creator !== creator).length,
    recommended_has_more: recommended.length < playlists.filter((playlist) => playlist.creator && playlist.creator !== creator).length,
    liked_tracks: await enrichTracks(liked.tracks, 36),
    liked_total: liked.total,
    liked_has_more: liked.tracks.length < liked.total,
  };
}

async function getPlaylistViewData(id) {
  const detail = await getPlaylistDetail(id, 80);
  const tracks = await enrichTracks(detail.tracks ?? [], 80);
  return {
    success: true,
    playlist: {
      id: String(detail.id),
      name: detail.name,
      description: detail.description ?? "",
      creator: typeof detail.creator === "string" ? detail.creator : detail.creator?.name ?? "",
      trackCount: detail.trackCount ?? tracks.length,
      coverUrl: detail.coverUrl ?? "",
    },
    tracks,
  };
}

function parseLrc(lrc) {
  const lines = [];
  for (const rawLine of lrc.split(/\r?\n/)) {
    const matches = [...rawLine.matchAll(/\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]/g)];
    if (matches.length === 0) continue;
    const text = rawLine.replace(/\[[^\]]+\]/g, "").trim();
    for (const match of matches) {
      const minutes = Number(match[1]);
      const seconds = Number(match[2]);
      const fraction = match[3] ? Number(match[3].padEnd(3, "0").slice(0, 3)) / 1000 : 0;
      lines.push({ time: minutes * 60 + seconds + fraction, text });
    }
  }
  return lines.sort((a, b) => a.time - b.time);
}

function extractLrcText(value) {
  if (!value) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    return value.lyric ?? value.lrc ?? value.text ?? value.content ?? "";
  }
  return "";
}

function mergeTranslatedLyrics(lines, translatedLines) {
  if (!translatedLines.length) return lines;
  const used = new Set();
  return lines.map((line) => {
    let bestIndex = -1;
    let bestDelta = Infinity;
    for (let i = 0; i < translatedLines.length; i += 1) {
      if (used.has(i)) continue;
      const translated = translatedLines[i];
      if (!translated.text || translated.text === line.text) continue;
      const delta = Math.abs(translated.time - line.time);
      if (delta < bestDelta) {
        bestDelta = delta;
        bestIndex = i;
      }
    }
    if (bestIndex >= 0 && bestDelta <= 0.35) {
      used.add(bestIndex);
      return { ...line, translation: translatedLines[bestIndex].text };
    }
    return line;
  });
}

function lyricLineText(line) {
  return [line.text, line.translation].filter(Boolean).join(" / ");
}

function isCreditLine(text) {
  if (!text) return true;
  return /^(作词|作曲|编曲|制作人|配唱制作|键盘|和声|录音|混音|OP|SP|By)\s*:/.test(text);
}

function firstLyricLines(lines, count) {
  return lines.filter((line) => !isCreditLine(line.text)).map(lyricLineText).slice(0, count);
}

function upcomingLyricLines(lines, positionSeconds, count = 6) {
  const meaningful = lines.filter((line) => !isCreditLine(line.text));
  if (meaningful.length === 0) return [];
  const safeCount = Math.max(1, Number(count || 6));
  let index = meaningful.findIndex((line) => line.time >= positionSeconds);
  if (index < 0) index = Math.max(0, meaningful.length - safeCount);
  return meaningful.slice(index, index + safeCount).map(lyricLineText);
}

function inferStyle(track, explicitStyle, wikiMetadata = null) {
  if (wikiMetadata?.genre) return wikiMetadata.genre;
  if (Array.isArray(wikiMetadata?.genres) && wikiMetadata.genres.length > 0) return wikiMetadata.genres.join("、");
  if (explicitStyle?.trim()) return explicitStyle.trim();
  const text = `${track.name} ${track.artist} ${track.album}`.toLowerCase();
  if (/林俊杰|周杰伦|陈奕迅|五月天/.test(text)) return "华语流行";
  if (/陈萝莉|foggy romance|summertime vagabond/.test(text)) return "独立流行 / 电子流行";
  if (/live|演唱会/.test(text)) return "现场流行";
  return "暂未识别";
}

function styleSource(state) {
  if (state?.wiki?.genre || state?.wiki?.genres?.length) return "netease_song_wiki";
  if (state?.style && state.style !== "暂未识别") return "fallback";
  return "unknown";
}

function playbackInfo(state) {
  return {
    id: state.id,
    name: state.name,
    artist: state.artist,
    album: state.album,
    durationMs: state.durationMs ?? null,
    coverUrl: state.coverUrl ?? "",
    style: state.style,
    style_source: styleSource(state),
    song_wiki: state.wiki ?? null,
  };
}

function activePendingPlaybackState() {
  if (!pendingPlaybackState) return null;
  if (Date.now() <= pendingPlaybackExpiresAt) return pendingPlaybackState;
  pendingPlaybackState = null;
  pendingPlaybackExpiresAt = 0;
  return null;
}

function clearPendingPlaybackState() {
  pendingPlaybackState = null;
  pendingPlaybackExpiresAt = 0;
}

function clearReplayRequestGuard() {
  replayRequestGuard = null;
}

function buildStartContext(state) {
  return `我们正在一起听歌，你现在跟我一起听${state.name}，曲风是${state.style}，歌手是${state.artist}，前4句歌词是${state.firstLyrics.join(" / ")}`;
}

function buildCurrentContext(state, currentLines) {
  return `我们正在一起听歌，你现在跟我一起听${state.name}，曲风是${state.style}，歌手是${state.artist}，当前的6句歌词是${currentLines.join(" / ")}`;
}

async function getPlayerStatus() {
  const { stdout } = await runNetease(["--pretty", "player", "status"], { timeout: 20000 });
  return parseJson(stdout, "neteasecli player status");
}

function statusLooksEnded(statusData, fallbackDurationMs = 0) {
  const position = Number(statusData?.position ?? 0);
  const duration = Number(statusData?.duration ?? 0) || (Number(fallbackDurationMs || 0) / 1000);
  if (!duration || !Number.isFinite(position)) return false;
  return position >= Math.max(0, duration - 0.5);
}

async function currentListeningContext(_before = 0, after = 6) {
  const pendingState = activePendingPlaybackState();
  if (pendingState) {
    const pendingMode = pendingState.pendingMode ?? "playback_start";
    const pendingSince = Number(pendingState.pendingSince ?? 0);
    const pendingAge = pendingSince ? Date.now() - pendingSince : 0;
    if (pendingAge > 900 || pendingMode === "replay_wait") {
      const status = await getPlayerStatus().catch(() => null);
      const statusData = status?.data;
      const statusDuration = Number(statusData?.duration ?? 0);
      const stateDuration = Number(pendingState.durationMs ?? 0) / 1000;
      const statusTitle = String(statusData?.title ?? "").trim().toLowerCase();
      const pendingTitle = String(pendingState.name ?? "").trim().toLowerCase();
      const titleMatches = Boolean(
        statusTitle &&
        pendingTitle &&
        (statusTitle === pendingTitle || statusTitle.includes(pendingTitle) || pendingTitle.includes(statusTitle))
      );
      const titleUnavailable = !statusTitle || !pendingTitle;
      const durationMatches = !statusDuration || !stateDuration || Math.abs(statusDuration - stateDuration) <= 2.5;
      const startPosition = Number(statusData?.position ?? 0);
      const canReleasePending = pendingMode === "replay_wait"
        ? pendingAge > 3200 && Boolean(statusData?.playing) && !statusData?.paused && startPosition <= 5
        : Boolean(statusData?.playing) && !statusData?.paused && durationMatches && (titleMatches || (titleUnavailable && startPosition <= 8 && pendingAge > 1500));
      if (canReleasePending) {
        clearPendingPlaybackState();
        const position = Number(statusData?.position ?? 0);
        const duration = stateDuration || statusDuration;
        const currentLines = upcomingLyricLines(pendingState.lyrics ?? [], position, Number(after || 6));
        return {
          success: true,
          active: true,
          paused: false,
          position,
          duration,
          positionFormatted: statusData?.positionFormatted,
          durationFormatted: statusData?.durationFormatted,
          status: statusData,
          playback: playbackInfo(pendingState),
          current_lyrics: currentLines,
          lyrics: pendingState.lyrics ?? [],
          ai_context: buildCurrentContext(pendingState, currentLines),
        };
      }
    }
    const currentLines = upcomingLyricLines(pendingState.lyrics ?? [], 0, Number(after || 6));
    return {
      success: true,
      active: false,
      paused: false,
      position: 0,
      duration: Number(pendingState.durationMs ?? 0) / 1000,
      positionFormatted: "0:00",
      durationFormatted: "",
      status: { playing: false, paused: false, pending: true, pendingMode, position: 0 },
      playback: playbackInfo(pendingState),
      current_lyrics: currentLines,
      lyrics: pendingState.lyrics ?? [],
      ai_context: buildCurrentContext(pendingState, currentLines),
    };
  }
  const state = await readState();
  if (!state) {
    return { success: true, active: false, ai_context: "" };
  }
  const status = await getPlayerStatus();
  const position = Number(status?.data?.position ?? 0);
  const duration = (Number(state.durationMs ?? 0) / 1000) || Number(status?.data?.duration ?? 0);
  const currentLines = upcomingLyricLines(state.lyrics ?? [], position, Number(after || 6));
  return {
    success: true,
    active: Boolean(status?.data?.playing),
    paused: Boolean(status?.data?.paused),
    position,
    duration,
    positionFormatted: status?.data?.positionFormatted,
    durationFormatted: status?.data?.durationFormatted,
    status: status?.data,
    playback: playbackInfo(state),
    current_lyrics: currentLines,
    lyrics: state.lyrics ?? [],
    ai_context: buildCurrentContext(state, currentLines),
  };
}

async function restartCachedTrack(reason = "restart") {
  const state = await readState();
  if (!state?.id) {
    throw new Error("No cached track to restart");
  }
  const restarted = await playTrackById(state.id, {
    quality: state.quality ?? "exhigh",
    style: state.style,
  });
  return {
    success: true,
    mode: "restart",
    reason,
    message: "Restarted the current song.",
    playback: playbackInfo(restarted),
    lyrics: restarted.lyrics ?? [],
    position: 0,
    duration: Number(restarted.durationMs ?? 0) / 1000,
    active: true,
    paused: false,
  };
}

async function playTrackById(id, { quality = "exhigh", style = "" } = {}) {
  return withPlaybackLock(async () => {
    const [detail, lyricData, wikiMetadata] = await Promise.all([
      getTrackDetail(id),
      getLyrics(id),
      getTrackWikiMetadata(id).catch((error) => ({
        id: String(id),
        source: "netease_song_wiki",
        genres: [],
        genre: "",
        recommended_tags: [],
        language: "",
        error: String(error.message ?? error),
      })),
    ]);
    const resolvedStyle = inferStyle(detail, style, wikiMetadata);
    const state = {
      id: String(id),
      name: detail.name,
      artist: detail.artist,
      album: detail.album,
      durationMs: detail.durationMs,
      coverUrl: detail.albumPicUrl,
      style: resolvedStyle,
      firstLyrics: firstLyricLines(lyricData.lines, 4),
      lyrics: lyricData.lines,
      wiki: wikiMetadata,
      quality,
      startedAt: new Date().toISOString(),
    };
    pendingPlaybackState = { ...state, pendingMode: "playback_start", pendingSince: Date.now() };
    pendingPlaybackExpiresAt = Date.now() + 60000;
    clearReplayRequestGuard();
    await writeState(state);
    try {
      await stopActivePlaybackBestEffort();
      await delay(250);
      await runNetease(["track", "play", String(id), "--quality", quality], { timeout: 45000 });
      await writeState(state);
      return state;
    } catch (error) {
      if (pendingPlaybackState?.id === String(id)) {
        clearPendingPlaybackState();
      }
      throw error;
    }
  });
}

async function replayCurrentTrackFromStart(id) {
  const requestedId = String(id);
  const now = Date.now();
  if (replayRequestGuard?.id === requestedId && now < replayRequestGuard.expiresAt) {
    const guardedState = await readState();
    if (guardedState?.id && String(guardedState.id) === requestedId) return guardedState;
  }
  return withPlaybackLock(async () => {
    const lockedNow = Date.now();
    if (replayRequestGuard?.id === requestedId && lockedNow < replayRequestGuard.expiresAt) {
      const guardedState = await readState();
      if (guardedState?.id && String(guardedState.id) === requestedId) return guardedState;
    }
    const state = await readState();
    if (!state?.id || String(state.id) !== requestedId) {
      throw new Error("Cached track does not match the requested replay target");
    }
    const replayState = { ...state, startedAt: new Date().toISOString() };
    replayRequestGuard = { id: requestedId, expiresAt: Date.now() + 8000 };
    pendingPlaybackState = { ...replayState, pendingMode: "replay_wait", pendingSince: Date.now() };
    pendingPlaybackExpiresAt = Date.now() + 15000;
    await writeState(replayState);
    try {
      await delay(3000);
      try {
        await runNetease(["--pretty", "player", "seek", "--absolute", "0"], { timeout: 10000 });
      } catch {
        await runNetease(["track", "play", requestedId, "--quality", replayState.quality ?? "exhigh"], { timeout: 45000 });
      }
      await delay(150);
      const status = await getPlayerStatus().catch(() => null);
      if (status?.data?.paused) {
        await runNetease(["--pretty", "player", "pause"], { timeout: 10000 });
      }
      return replayState;
    } catch (error) {
      if (pendingPlaybackState?.id === requestedId) {
        clearPendingPlaybackState();
      }
      throw error;
    }
  });
}

async function toggleOrRestartPlayback() {
  try {
    const status = await getPlayerStatus().catch(() => null);
    const statusData = status?.data;
    const state = await readState();
    if (!statusData?.playing && !statusData?.paused) {
      return restartCachedTrack("no_active_player");
    }
    if (statusLooksEnded(statusData, state?.durationMs)) {
      return restartCachedTrack("ended");
    }
    const result = parseJson((await runNetease(["--pretty", "player", "pause"])).stdout, "neteasecli player pause");
    return { success: true, mode: "toggle", result };
  } catch (error) {
    try {
      return await restartCachedTrack("pause_failed");
    } catch {
      throw error;
    }
  }
}

function playerHtml() {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NetEase Music Player</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #221f1c;
      --panel: rgba(46, 42, 37, .84);
      --panel-2: rgba(56, 49, 42, .82);
      --soft: #38322d;
      --muted: #b9aa9a;
      --text: #fff8ef;
      --line: rgba(255,255,255,.09);
      --accent: #ff9f3f;
      --accent-2: #ffb35c;
      --red: #ef5d54;
      --player-row: clamp(78px, 10vh, 92px);
      --page-pad-x: clamp(14px, 2.1vw, 24px);
      --page-pad-y: clamp(10px, 1.8vh, 22px);
      --panel-pad: clamp(12px, 1.9vh, 18px);
      --panel-gap: clamp(10px, 1.8vh, 18px);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 12% 0%, rgba(255, 143, 45, .30), transparent 34%),
        radial-gradient(circle at 88% 12%, rgba(255, 197, 106, .16), transparent 30%),
        #211f1d;
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      padding: 0;
      overflow: hidden;
    }
    button { font: inherit; }
    .shell {
      width: 100vw;
      height: 100vh;
      margin: 0;
      display: grid;
      grid-template-columns: 1fr;
      grid-template-rows: minmax(0, 1fr) var(--player-row);
      background:
        linear-gradient(145deg, rgba(36,33,30,.96), rgba(27,25,23,.98));
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 0;
      overflow: hidden;
      box-shadow: 0 26px 60px rgba(0,0,0,.42);
    }
    aside {
      display: none !important;
      grid-row: 1 / 3;
      background: #0d1218;
      border-right: 1px solid var(--line);
      padding: 22px 18px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      min-width: 0;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 18px; }
    .brand-dot {
      width: 34px;
      height: 34px;
      border-radius: 7px;
      display: grid;
      place-items: center;
      background: linear-gradient(135deg, #f3b04b, #d84d43);
      color: #111;
    }
    .nav-title { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
    .nav-list { display: grid; gap: 10px; }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 9px;
      color: #b7c0ca;
      min-height: 40px;
      padding: 0 9px;
      border-radius: 6px;
      font-size: 14px;
    }
    .nav-item.active { background: #172029; color: #fff; }
    .mini-playlists {
      overflow: hidden;
      display: grid;
      gap: 12px;
    }
    .mini {
      display: grid;
      grid-template-columns: 42px 1fr;
      gap: 10px;
      align-items: center;
      min-width: 0;
    }
    .mini img, .mini .fallback {
      width: 42px;
      height: 42px;
      border-radius: 7px;
      object-fit: cover;
      background: var(--soft);
      cursor: pointer;
    }
    .mini strong, .mini span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .mini strong { font-size: 13px; }
    .mini span { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .content {
      grid-column: 1;
      min-width: 0;
      overflow: hidden;
      display: grid;
      grid-template-rows: auto 1fr;
      position: relative;
      background:
        radial-gradient(circle at 20% 2%, rgba(255, 143, 45, .18), transparent 32%),
        linear-gradient(180deg, #25221f 0%, #1d1b19 100%);
    }
    .content.playing {
      grid-template-rows: 1fr;
    }
    .content.playing .topbar {
      display: none;
    }
    .topbar {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      align-items: center;
      justify-items: end;
      padding: var(--page-pad-y) var(--page-pad-x) clamp(6px, 1.1vh, 10px);
    }
    .search {
      display: flex;
      gap: 8px;
      align-items: center;
      min-width: min(430px, 42vw);
      background: rgba(255,255,255,.06);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 7px 8px 7px 16px;
    }
    .search .icon-btn svg {
      width: 15px;
      height: 15px;
      display: block;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }
    input {
      width: 100%;
      min-width: 0;
      border: 0;
      outline: 0;
      background: transparent;
      color: var(--text);
      font-size: 13px;
    }
    .icon-btn, .play-small {
      border: 0;
      display: grid;
      place-items: center;
      color: var(--text);
      background: rgba(255,255,255,.08);
      cursor: pointer;
      padding: 0;
    }
    .icon-btn {
      width: 28px;
      height: 28px;
      border-radius: 50%;
    }
    .hero {
      overflow: hidden;
      overscroll-behavior: contain;
      min-height: 0;
      display: grid;
      padding: clamp(8px, 1.5vh, 14px) var(--page-pad-x) clamp(12px, 2.3vh, 24px);
    }
    #homeView {
      height: 100%;
      min-height: 0;
      display: grid;
      grid-template-rows: minmax(142px, .58fr) minmax(0, 1.42fr);
      gap: var(--panel-gap);
      overflow: hidden;
    }
    .hidden { display: none !important; }
    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: var(--panel-pad);
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: clamp(10px, 1.8vh, 18px);
      flex: 0 0 auto;
    }
    .section h2 {
      margin: 0;
      font-size: clamp(15px, 1.9vh, 17px);
      line-height: 1.2;
    }
    #homeView > .section:first-child {
      padding: 14px 18px 12px;
    }
    #homeView > .section:first-child .section-head {
      margin-bottom: 10px;
    }
    .see {
      border: 0;
      background: transparent;
      color: var(--muted);
      font-size: 12px;
      cursor: pointer;
    }
    .playlist-strip {
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: minmax(112px, 1fr);
      gap: clamp(10px, 1.5vw, 20px);
      overflow-x: auto;
      overflow-y: hidden;
      min-height: 132px;
      padding-bottom: 4px;
    }
    .playlist-stack {
      grid-auto-flow: row;
      grid-auto-columns: unset;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px 12px;
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      align-content: start;
      padding-right: 4px;
    }
    .playlist-card {
      min-width: 0;
      border: 0;
      background: transparent;
      color: inherit;
      padding: 0;
      text-align: left;
      cursor: pointer;
    }
    .cover {
      width: 100%;
      aspect-ratio: 1;
      border-radius: 8px;
      object-fit: cover;
      display: block;
      background: linear-gradient(135deg, #ff9f3f, #6a4632);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.06);
    }
    .playlist-card strong, .playlist-card span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .playlist-card strong { font-size: 13px; margin-top: 10px; }
    .playlist-card span { color: var(--muted); font-size: 11px; margin-top: 3px; }
    .grid {
      margin-top: 0;
      display: grid;
      grid-template-columns: minmax(220px, 320px) minmax(0, 1fr);
      gap: var(--panel-gap);
      height: 100%;
      min-height: 0;
      overflow: hidden;
    }
    .recs {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px 12px;
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      align-items: start;
      align-content: start;
      padding-right: 4px;
    }
    .recs-wide {
      grid-template-columns: repeat(var(--rec-columns, 7), minmax(0, 1fr));
      grid-auto-rows: minmax(0, 1fr);
      gap: 9px;
      overflow: hidden;
      padding-right: 0;
      height: 100%;
    }
    .recs-wide .rec {
      aspect-ratio: auto;
      height: 100%;
      min-height: 0;
    }
    .rec-page-loading {
      grid-column: 1 / -1;
      height: 100%;
      min-height: 96px;
      display: grid;
      place-items: center;
    }
    .pager-actions {
      display: flex;
      gap: 14px;
      align-items: center;
    }
    .rec {
      display: block;
      position: relative;
      aspect-ratio: 1 / 1.2;
      min-width: 0;
      width: 100%;
      border: 0;
      --rec-rgb: 177, 103, 32;
      background: linear-gradient(135deg, #ff9f3f, #6a4632);
      color: var(--text);
      padding: 0;
      font-size: 12px;
      line-height: 1.2;
      text-align: left;
      cursor: pointer;
      overflow: hidden;
      border-radius: 7px;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.08);
    }
    .rec-cover {
      width: 100%;
      height: 100%;
      aspect-ratio: auto;
      border-radius: inherit;
      object-fit: cover;
      display: block;
      background: linear-gradient(135deg, #ff9f3f, #6a4632);
    }
    .rec-count {
      position: absolute;
      top: 7px;
      left: 8px;
      z-index: 2;
      color: #fff;
      font-size: 11px;
      font-weight: 800;
      text-shadow: 0 1px 5px rgba(0,0,0,.58);
    }
    .rec-title {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 2;
      display: flex;
      align-items: flex-end;
      justify-content: flex-start;
      overflow: hidden;
      box-sizing: border-box;
      min-height: 38px;
      max-height: 58%;
      padding: 10px 8px 7px;
      color: #fff;
      background:
        linear-gradient(180deg, rgba(var(--rec-rgb), 0), rgba(33, 27, 23, .58) 26%, rgba(var(--rec-rgb), .92));
      font-size: 12px;
      font-weight: 700;
      text-align: left;
      text-shadow: 0 1px 5px rgba(0,0,0,.36);
      word-break: break-word;
      overflow-wrap: anywhere;
    }
    .charts {
      flex: 1 1 auto;
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      padding-right: 4px;
    }
    .playlist-detail,
    .search-view {
      --detail-grid: 42px 42px clamp(300px, 46vw, 500px) clamp(120px, 18vw, 220px) 48px minmax(0, 1fr) 32px;
    }
    .playlist-detail {
      display: grid;
      gap: 18px;
      min-height: 0;
      overflow: auto;
      position: relative;
      padding-top: 34px;
    }
    .back-home {
      position: absolute;
      top: 0;
      left: 0;
      z-index: 2;
      padding: 0;
      min-height: 24px;
    }
    .search-view {
      display: grid;
      gap: 18px;
      min-height: 0;
      overflow: hidden;
      position: relative;
      padding-top: 34px;
    }
    .search-results {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
      padding-right: 4px;
    }
    .detail-hero {
      display: grid;
      grid-template-columns: clamp(132px, 17vw, 176px) minmax(0, 1fr);
      gap: clamp(14px, 2vw, 24px);
      align-items: start;
      padding: 4px 0 10px;
    }
    .detail-cover {
      width: clamp(132px, 17vw, 176px);
      aspect-ratio: 1;
      border-radius: 10px;
      object-fit: cover;
      background: linear-gradient(135deg, #8db6cf, #cad8c2);
      box-shadow: 0 18px 36px rgba(0,0,0,.24);
    }
    .detail-kicker {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }
    .detail-title {
      margin: 0;
      font-size: clamp(20px, 2.9vh, 26px);
      line-height: 1.2;
    }
    .detail-desc {
      margin: 10px 0 0;
      color: #b5bec9;
      line-height: 1.6;
      font-size: 13px;
      max-width: 720px;
    }
    .detail-meta {
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .detail-actions {
      display: flex;
      gap: 12px;
      margin-top: 22px;
      flex-wrap: wrap;
    }
    .primary-btn, .soft-btn {
      border: 0;
      border-radius: 8px;
      min-height: 38px;
      padding: 0 16px;
      cursor: pointer;
      color: var(--text);
    }
    .primary-btn {
      background: #ff405d;
      color: white;
      font-weight: 700;
    }
    .soft-btn {
      background: #172029;
      color: #cbd4df;
      border: 1px solid var(--line);
    }
    .detail-table-head {
      display: grid;
      grid-template-columns: var(--detail-grid);
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      box-sizing: border-box;
      padding: 0 18px 7px 0;
      border-bottom: 1px solid var(--line);
    }
    .detail-table-head span:nth-child(5),
    .detail-track .track-time {
      text-align: right;
    }
    .detail-table-head span:first-child {
      visibility: hidden;
    }
    .detail-table-head span:nth-child(6),
    .detail-track .play-small {
      grid-column: 7;
    }
    .detail-tracks {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
      padding-right: 4px;
    }
    .detail-track {
      display: grid;
      grid-template-columns: var(--detail-grid, 42px 42px clamp(300px, 46vw, 500px) clamp(120px, 18vw, 220px) 48px minmax(0, 1fr) 32px);
      gap: 10px;
      align-items: center;
      min-height: 50px;
      border-bottom: 1px solid var(--line);
      color: #d9dde2;
      font-size: 13px;
      padding-right: 18px;
    }
    .detail-track .track-cover {
      width: 38px;
      height: 38px;
    }
    .play-page {
      position: fixed;
      inset: 0 0 var(--player-row) 0;
      z-index: 9;
      height: auto;
      min-height: 0;
      border-radius: 0;
      padding: clamp(18px, 3vh, 26px) clamp(18px, 3vw, 34px);
      background:
        linear-gradient(90deg, rgba(7,10,16,.58), rgba(7,10,16,.9)),
        var(--player-color, #111923);
      overflow: hidden;
      overscroll-behavior: contain;
    }
    .play-page.opening {
      animation: playPageSlideUp .34s cubic-bezier(.32, .72, .22, 1) both;
    }
    .play-page.closing {
      animation: playPageSlideDown .34s cubic-bezier(.32, .72, .22, 1) forwards;
      pointer-events: none;
    }
    .play-page::before {
      content: "";
      position: absolute;
      inset: -40px;
      background: var(--player-cover, none) center / cover no-repeat;
      opacity: .18;
      filter: blur(28px);
      transform: scale(1.08);
    }
    .play-page > * { position: relative; z-index: 1; }
    .play-back {
      position: absolute;
      top: 32px;
      left: clamp(18px, 3vw, 34px);
      z-index: 30;
      border: 0;
      width: 42px;
      height: 42px;
      border-radius: 0;
      display: grid;
      place-items: center;
      background: transparent;
      color: #fff;
      cursor: pointer;
      margin: 0;
      box-shadow: none;
      transition: color .18s ease, transform .18s ease;
    }
    .play-back:hover {
      background: transparent;
      color: rgba(255,255,255,.76);
      transform: translateY(1px);
    }
    .play-back svg {
      width: 24px;
      height: 24px;
      display: block;
      stroke: currentColor;
      stroke-width: 2.8;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
      pointer-events: none;
    }
    .play-layout {
      display: grid;
      grid-template-columns:
        minmax(260px, min(38vw, clamp(280px, 39vh, 460px)))
        minmax(330px, min(46vw, clamp(360px, 47vh, 640px)));
      justify-content: center;
      gap: clamp(28px, 5.4vw, 92px);
      align-items: center;
      width: min(100%, 1320px);
      margin: 0 auto;
      height: 100%;
      min-height: 0;
      overflow: hidden;
      overscroll-behavior: contain;
    }
    .vinyl-wrap {
      width: min(clamp(280px, 39vh, 460px), 38vw, calc(100% - 24px));
      aspect-ratio: 1;
      border-radius: 50%;
      display: grid;
      place-items: center;
      justify-self: center;
      position: relative;
      isolation: isolate;
      overflow: visible;
      --wave-beat: 0;
      --wave-soft: 0;
      background:
        radial-gradient(circle at 50% 50%, rgba(255,255,255,.08) 0 7%, transparent 8%),
        radial-gradient(circle, #202632 0 31%, #090a0d 32% 68%, #151923 69% 100%);
      box-shadow:
        0 0 44px rgba(255,255,255,.12),
        0 0 88px rgba(244,250,255,.16),
        0 24px 54px rgba(0,0,0,.42);
      animation: vinylSpin 18s linear infinite;
      animation-play-state: paused;
    }
    .vinyl-wrap.spinning {
      animation-play-state: running;
    }
    .vinyl-wrap::before,
    .vinyl-wrap::after {
      content: "";
      position: absolute;
      border-radius: 50%;
      pointer-events: none;
    }
    .vinyl-wrap::before {
      inset: 0;
      z-index: 2;
      background:
        radial-gradient(circle at 34% 28%, rgba(255,255,255,.09), transparent 0 9%, transparent 16%),
        linear-gradient(118deg, transparent 0 41%, rgba(255,255,255,.08) 45%, rgba(255,255,255,.025) 48%, transparent 53%),
        repeating-radial-gradient(circle, rgba(255,255,255,.075) 0 1px, transparent 1px 6px),
        radial-gradient(circle, transparent 0 31%, rgba(255,255,255,.035) 31% 32%, transparent 33% 100%),
        radial-gradient(circle, #14171f 0 31%, #050608 32% 68%, #11151d 69% 100%);
      box-shadow:
        inset 0 0 0 1px rgba(255,255,255,.035),
        inset 0 0 28px rgba(255,255,255,.045),
        inset 0 -20px 42px rgba(0,0,0,.52);
    }
    .vinyl-wrap::after {
      inset: -26px;
      z-index: -1;
      background:
        radial-gradient(circle, rgba(255,255,255,.24), rgba(232,244,255,.14) 32%, transparent 66%);
      filter: blur(10px);
      opacity: .72;
    }
    .vinyl-halo {
      position: absolute;
      inset: -28px;
      border-radius: 50%;
      pointer-events: none;
      z-index: 0;
      opacity: calc(.34 + var(--wave-beat) * .34);
      transform: scale(calc(1 + var(--wave-beat) * .052));
      transition: opacity .18s ease-out, transform .18s ease-out;
      background:
        radial-gradient(circle, rgba(255,255,255,.5) 0 28%, rgba(242,248,255,.22) 44%, transparent 70%);
      filter: blur(calc(12px + var(--wave-beat) * 8px));
    }
    .vinyl-halo.outer {
      inset: -58px;
      opacity: calc(.18 + var(--wave-soft) * .34);
      transform: scale(calc(1 + var(--wave-soft) * .09));
      background:
        radial-gradient(circle, rgba(255,255,255,.26) 0 30%, rgba(240,248,255,.18) 48%, transparent 74%);
      filter: blur(calc(22px + var(--wave-soft) * 16px));
    }
    .vinyl-wrap:not(.spinning) .vinyl-halo {
      opacity: .16;
      transform: scale(1);
      filter: blur(14px);
    }
    .vinyl-wrap:not(.spinning) .vinyl-halo.outer {
      opacity: .08;
      filter: blur(24px);
    }
    .play-cover {
      width: 48%;
      aspect-ratio: 1;
      border-radius: 50%;
      object-fit: cover;
      position: relative;
      z-index: 3;
      box-shadow:
        0 0 0 10px rgba(255,255,255,.025),
        0 0 0 12px rgba(0,0,0,.18),
        0 10px 20px rgba(0,0,0,.28);
    }
    @keyframes vinylSpin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    @keyframes playPageSlideUp {
      from {
        transform: translateY(105%);
        opacity: .25;
        filter: blur(2px);
      }
      to {
        transform: translateY(0);
        opacity: 1;
        filter: blur(0);
      }
    }
    @keyframes playPageSlideDown {
      from {
        transform: translateY(0);
        opacity: 1;
        filter: blur(0);
      }
      to {
        transform: translateY(105%);
        opacity: .25;
        filter: blur(2px);
      }
    }
    .play-title {
      margin: 0;
      font-size: clamp(28px, 3.55vh, 42px);
      line-height: 1.2;
      max-width: 660px;
    }
    .play-sub {
      margin-top: clamp(10px, 1.45vh, 17px);
      color: rgba(234,240,246,.68);
      font-size: clamp(13px, 1.5vh, 17px);
    }
    .lyric-scroll {
      --lyric-center-pad: 150px;
      margin-top: clamp(24px, 3.35vh, 40px);
      height: clamp(320px, 44vh, 520px);
      box-sizing: border-box;
      position: relative;
      overflow: auto;
      overscroll-behavior: contain;
      overflow-anchor: none;
      scroll-behavior: smooth;
      padding: var(--lyric-center-pad) 8px var(--lyric-center-pad) 0;
      display: grid;
      align-content: start;
      gap: clamp(20px, 2.35vh, 30px);
      color: rgba(234,240,246,.38);
      font-size: clamp(16px, 1.9vh, 23px);
      line-height: 1.5;
    }
    .playlist-strip,
    .recs,
    .charts,
    .playlist-detail,
    .search-results,
    .queue-items,
    .detail-tracks,
    .lyric-scroll {
      scrollbar-width: thin;
      scrollbar-color: rgba(255,255,255,.78) transparent;
    }
    .playlist-strip::-webkit-scrollbar,
    .recs::-webkit-scrollbar,
    .charts::-webkit-scrollbar,
    .playlist-detail::-webkit-scrollbar,
    .search-results::-webkit-scrollbar,
    .queue-items::-webkit-scrollbar,
    .detail-tracks::-webkit-scrollbar,
    .lyric-scroll::-webkit-scrollbar {
      width: 3px;
      height: 3px;
    }
    .playlist-strip::-webkit-scrollbar-track,
    .recs::-webkit-scrollbar-track,
    .charts::-webkit-scrollbar-track,
    .playlist-detail::-webkit-scrollbar-track,
    .search-results::-webkit-scrollbar-track,
    .queue-items::-webkit-scrollbar-track,
    .detail-tracks::-webkit-scrollbar-track,
    .lyric-scroll::-webkit-scrollbar-track {
      background: transparent;
      border: 0;
    }
    .playlist-strip::-webkit-scrollbar-thumb,
    .recs::-webkit-scrollbar-thumb,
    .charts::-webkit-scrollbar-thumb,
    .playlist-detail::-webkit-scrollbar-thumb,
    .search-results::-webkit-scrollbar-thumb,
    .queue-items::-webkit-scrollbar-thumb,
    .detail-tracks::-webkit-scrollbar-thumb,
    .lyric-scroll::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,.78);
      border: 0;
      border-radius: 999px;
    }
    .playlist-strip::-webkit-scrollbar-button,
    .recs::-webkit-scrollbar-button,
    .charts::-webkit-scrollbar-button,
    .playlist-detail::-webkit-scrollbar-button,
    .search-results::-webkit-scrollbar-button,
    .queue-items::-webkit-scrollbar-button,
    .detail-tracks::-webkit-scrollbar-button,
    .lyric-scroll::-webkit-scrollbar-button {
      display: none;
      width: 0;
      height: 0;
      background: transparent;
    }
    .playlist-strip::-webkit-scrollbar-button:single-button,
    .recs::-webkit-scrollbar-button:single-button,
    .charts::-webkit-scrollbar-button:single-button,
    .playlist-detail::-webkit-scrollbar-button:single-button,
    .search-results::-webkit-scrollbar-button:single-button,
    .queue-items::-webkit-scrollbar-button:single-button,
    .detail-tracks::-webkit-scrollbar-button:single-button,
    .lyric-scroll::-webkit-scrollbar-button:single-button {
      display: none;
      width: 0;
      height: 0;
      background: transparent;
    }
    .playlist-strip::-webkit-scrollbar-corner,
    .recs::-webkit-scrollbar-corner,
    .charts::-webkit-scrollbar-corner,
    .playlist-detail::-webkit-scrollbar-corner,
    .search-results::-webkit-scrollbar-corner,
    .queue-items::-webkit-scrollbar-corner,
    .detail-tracks::-webkit-scrollbar-corner,
    .lyric-scroll::-webkit-scrollbar-corner {
      background: transparent;
    }
    .lyric-scroll {
      scrollbar-width: none;
      -ms-overflow-style: none;
    }
    .lyric-scroll::-webkit-scrollbar {
      display: none;
      width: 0;
      height: 0;
    }
    .lyric-row {
      display: grid;
      gap: 5px;
    }
    .lyric-original {
      color: rgba(234,240,246,.4);
      font-size: clamp(16px, 1.9vh, 23px);
      line-height: 1.45;
      font-weight: 500;
    }
    .lyric-translation {
      color: rgba(234,240,246,.28);
      font-size: clamp(13px, 1.5vh, 17px);
      line-height: 1.45;
      font-weight: 500;
    }
    .lyric-row.active .lyric-original {
      color: #fff;
      font-size: clamp(20px, 2.35vh, 29px);
      font-weight: 700;
    }
    .lyric-row.active .lyric-translation {
      color: rgba(255,255,255,.72);
      font-size: clamp(14px, 1.65vh, 19px);
      font-weight: 600;
    }
    .track {
      display: grid;
      grid-template-columns: 34px 46px minmax(180px, 1fr) minmax(110px, .7fr) 58px 36px 22px;
      gap: 12px;
      align-items: center;
      min-height: 62px;
      border-bottom: 1px solid var(--line);
      color: #d9dde2;
      font-size: 13px;
    }
    .charts .track {
      grid-template-columns: 28px 42px minmax(0, 1fr) 30px;
      gap: 10px;
    }
    .charts .track-artist,
    .charts .track-time,
    .charts .more {
      display: none;
    }
    .charts .play-small {
      width: 28px;
      height: 28px;
      background: transparent;
      border-radius: 0;
      color: var(--accent-2);
      font-size: 16px;
      line-height: 1;
    }
    .rank { color: var(--muted); text-align: right; }
    .track-cover {
      width: 42px;
      height: 42px;
      border-radius: 6px;
      object-fit: cover;
      background: var(--soft);
    }
    .track-main, .track-artist, .track-time {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .track-main strong, .track-main span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .track-main span, .track-artist, .track-time, .more { color: var(--muted); font-size: 12px; }
    .play-small {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      color: var(--accent);
    }
    .player {
      grid-column: 1;
      position: relative;
      z-index: 20;
      background:
        linear-gradient(90deg, rgba(36,36,38,.70), rgba(29,29,31,.58)),
        color-mix(in srgb, var(--footer-color, #222226) 34%, transparent);
      border: 1px solid rgba(255,255,255,.12);
      border-top-color: rgba(255,255,255,.18);
      border-radius: 14px;
      margin: 0 clamp(8px, 1.4vw, 14px) clamp(8px, 1.4vh, 12px);
      backdrop-filter: blur(20px) saturate(1.25);
      -webkit-backdrop-filter: blur(20px) saturate(1.25);
      box-shadow: 0 18px 42px rgba(0,0,0,.38);
      display: grid;
      grid-template-columns: minmax(170px, 1fr) auto minmax(150px, 1fr);
      gap: clamp(10px, 2vw, 22px);
      align-items: center;
      padding: clamp(8px, 1.2vh, 10px) clamp(10px, 1.6vw, 18px);
      min-width: 0;
    }
    .player-timeline {
      position: absolute;
      left: 10px;
      right: 10px;
      top: 0;
      height: 14px;
      cursor: pointer;
      z-index: 25;
    }
    .player-timeline::before {
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      top: 0;
      height: 2px;
      border-radius: 999px;
      background: rgba(255,255,255,.16);
    }
    .player-timeline-fill {
      position: absolute;
      left: 0;
      top: 0;
      width: var(--progress, 0%);
      height: 2px;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(255,159,63,.92), #ff7a22);
      box-shadow: 0 0 10px rgba(255, 139, 50, .28);
      transition: width .12s linear;
    }
    .player-timeline-tip {
      position: absolute;
      left: var(--hover-x, 0%);
      top: -28px;
      z-index: 40;
      transform: translateX(-50%);
      display: none;
      white-space: nowrap;
      padding: 4px 8px;
      border-radius: 6px;
      background: rgba(22,20,18,.9);
      color: #fff8ef;
      border: 1px solid rgba(255,255,255,.12);
      font-size: 11px;
      box-shadow: 0 10px 24px rgba(0,0,0,.34);
      pointer-events: none;
    }
    .player-timeline:hover .player-timeline-tip {
      display: block;
    }
    .now-playing {
      display: grid;
      grid-template-columns: clamp(40px, 5.4vh, 46px) 1fr;
      gap: clamp(8px, 1.2vw, 12px);
      align-items: center;
      min-width: 0;
    }
    .now-playing img, .now-playing .fallback {
      width: clamp(40px, 5.4vh, 46px);
      height: clamp(40px, 5.4vh, 46px);
      border-radius: 8px;
      object-fit: cover;
      background: var(--soft);
      cursor: pointer;
    }
    .now-playing strong, .now-playing span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .now-playing strong { font-size: 12px; }
    .now-playing span { color: var(--muted); font-size: 11px; margin-top: 3px; }
    .controls {
      display: flex;
      gap: clamp(8px, 1.4vw, 16px);
      align-items: center;
      justify-content: center;
      min-width: clamp(124px, 18vw, 150px);
      flex-wrap: nowrap;
    }
    .controls .icon-btn {
      width: 28px;
      height: 28px;
      min-width: 28px;
      min-height: 28px;
      flex: 0 0 28px;
      aspect-ratio: 1;
      border-radius: 50%;
      background: transparent;
      color: #aeb5bd;
      font-size: 0;
      line-height: 1;
    }
    .controls svg {
      width: 20px;
      height: 20px;
      display: block;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }
    .controls .icon-btn:hover {
      color: var(--text);
      background: rgba(255,255,255,.06);
    }
    .controls .mode-control.active {
      color: var(--accent-2);
      background: transparent;
    }
    .controls .skip-icon {
      color: #aab2bc;
    }
    .controls .list-icon {
      border-radius: 7px;
    }
    .icon-btn.primary-control {
      width: 42px;
      height: 42px;
      min-width: 42px;
      min-height: 42px;
      flex-basis: 42px;
      aspect-ratio: 1;
      background: linear-gradient(135deg, #ff9f3f, #ff7a22);
      color: #fff;
      font-weight: 800;
      box-shadow: 0 10px 24px rgba(255, 139, 50, .34);
      padding: 0;
    }
    .icon-btn.primary-control svg {
      width: 23px;
      height: 23px;
      fill: currentColor;
      stroke: none;
      transform: translateX(.5px);
    }
    .icon-btn.primary-control.is-playing svg {
      width: 20px;
      height: 20px;
      transform: none;
    }
    .icon-btn.primary-control:hover {
      background: linear-gradient(135deg, #ffad4f, #ff8734);
      color: #fff;
    }
    .wave {
      display: none;
    }
    .progress-wrap {
      display: grid;
      grid-template-columns: auto auto 18px minmax(74px, 128px);
      gap: clamp(6px, 1vw, 10px);
      align-items: center;
      min-width: 0;
      justify-self: end;
      color: var(--muted);
      font-size: 11px;
    }
    .queue-toggle-mini {
      width: 24px;
      height: 24px;
      border: 0;
      border-radius: 7px;
      display: grid;
      place-items: center;
      background: transparent;
      color: #c8d0da;
      cursor: pointer;
    }
    .queue-toggle-mini:hover,
    .queue-toggle-mini.active {
      color: var(--accent-2);
      background: transparent;
    }
    .queue-toggle-mini svg {
      width: 17px;
      height: 17px;
      display: block;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }
    .volume-icon {
      color: #c8d0da;
      display: grid;
      place-items: center;
    }
    .volume-icon svg {
      width: 16px;
      height: 16px;
      display: block;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }
    .volume-slider {
      width: 100%;
      height: 12px;
      appearance: none;
      -webkit-appearance: none;
      background: transparent;
      cursor: pointer;
      --volume: 80%;
    }
    .volume-slider::-webkit-slider-runnable-track {
      height: 3px;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent-2) 0 var(--volume), rgba(255,255,255,.18) var(--volume) 100%);
    }
    .volume-slider::-webkit-slider-thumb {
      -webkit-appearance: none;
      width: 0;
      height: 0;
      margin-top: 0;
      border: 0;
      background: transparent;
      box-shadow: none;
    }
    .volume-slider::-moz-range-track {
      height: 3px;
      border-radius: 999px;
      background: rgba(255,255,255,.18);
    }
    .volume-slider::-moz-range-progress {
      height: 3px;
      border-radius: 999px;
      background: var(--accent-2);
    }
    .volume-slider::-moz-range-thumb {
      width: 0;
      height: 0;
      border: 0;
      background: transparent;
    }
    .queue-panel {
      position: absolute;
      right: 18px;
      bottom: 72px;
      z-index: 8;
      width: min(360px, calc(100vw - 36px));
      max-height: min(420px, calc(100vh - 150px));
      padding: 14px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,.12);
      background:
        linear-gradient(180deg, rgba(35,32,29,.88), rgba(22,21,20,.9)),
        color-mix(in srgb, var(--footer-color, #222226) 30%, transparent);
      backdrop-filter: blur(20px) saturate(1.2);
      -webkit-backdrop-filter: blur(20px) saturate(1.2);
      box-shadow: 0 22px 54px rgba(0,0,0,.46);
      display: flex;
      flex-direction: column;
      gap: 10px;
      overflow: hidden;
    }
    .queue-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      color: var(--text);
    }
    .queue-head strong { font-size: 14px; }
    .queue-head span { color: var(--muted); font-size: 11px; }
    .queue-items {
      overflow: auto;
      min-height: 0;
      padding-right: 4px;
      display: grid;
      gap: 4px;
    }
    .queue-item {
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      min-height: 42px;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: inherit;
      text-align: left;
      cursor: pointer;
      padding: 5px 8px;
    }
    .queue-item:hover,
    .queue-item.active {
      background: rgba(255,255,255,.08);
    }
    .queue-item.active {
      color: #fff4e8;
      box-shadow: inset 3px 0 0 var(--accent-2);
    }
    .queue-index {
      color: var(--muted);
      font-size: 11px;
      text-align: right;
    }
    .queue-main {
      min-width: 0;
    }
    .queue-main strong,
    .queue-main span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .queue-main strong { font-size: 12px; }
    .queue-main span { color: var(--muted); font-size: 11px; margin-top: 2px; }
    .queue-time { color: var(--muted); font-size: 11px; }
    .lyrics-line {
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      text-align: right;
    }
    .empty { color: var(--muted); font-size: 12px; padding: 8px 0; }
    @media (max-width: 860px) {
      :root {
        --page-pad-x: 14px;
        --panel-pad: 12px;
        --panel-gap: 10px;
      }
      .grid {
        grid-template-columns: minmax(190px, .42fr) minmax(0, .58fr);
      }
      .playlist-stack {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 9px 8px;
      }
      .playlist-stack .cover {
        border-radius: 7px;
      }
      .playlist-stack .playlist-card strong {
        font-size: 12px;
        margin-top: 7px;
      }
      .playlist-stack .playlist-card span {
        font-size: 10px;
      }
      .charts .track {
        grid-template-columns: 24px 38px minmax(0, 1fr) 26px;
        gap: 8px;
        min-height: 54px;
      }
      .play-layout {
        grid-template-columns: minmax(240px, .42fr) minmax(300px, .58fr);
        gap: 34px;
      }
      .vinyl-wrap {
        width: min(clamp(240px, 32vmin, 340px), calc(100% - 24px));
      }
      .play-title {
        font-size: clamp(24px, 3.2vh, 34px);
      }
      .lyric-scroll {
        --lyric-center-pad: 112px;
        height: clamp(260px, 40vh, 430px);
      }
    }
    @media (max-width: 720px) {
      .topbar {
        justify-items: stretch;
      }
      .search {
        min-width: 0;
        width: 100%;
      }
      .player {
        grid-template-columns: minmax(120px, 1fr) auto minmax(92px, .72fr);
      }
      .progress-wrap {
        grid-template-columns: auto auto 18px minmax(62px, 96px);
      }
    }
    @media (max-width: 560px) {
      :root {
        --player-row: 82px;
      }
      body { padding: 0; }
      .shell {
        height: 100vh;
        grid-template-columns: 1fr;
        grid-template-rows: minmax(0, 1fr) var(--player-row);
      }
      .player {
        grid-column: 1;
        grid-template-columns: minmax(96px, 1fr) auto minmax(74px, .62fr);
        gap: 8px;
        border-radius: 10px;
      }
      .now-playing span { display: none; }
      .controls .icon-btn:not(.primary-control):not(#prevTrack):not(#nextTrack) { display: none; }
      .grid {
        grid-template-columns: minmax(0, .46fr) minmax(0, .54fr);
      }
      .playlist-stack {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .topbar { grid-template-columns: 1fr; }
      .search { min-width: 0; }
      .track { grid-template-columns: 24px 34px minmax(80px, 1fr) 30px 18px; }
      .track-artist, .track-time { display: none; }
      .detail-hero { grid-template-columns: 1fr; }
      .detail-cover { width: min(176px, 52vw); }
      .detail-table-head, .detail-track { grid-template-columns: 34px 42px minmax(0, 1fr) 34px; }
      .detail-table-head span:nth-child(4),
      .detail-table-head span:nth-child(5),
      .detail-track > div:nth-child(4),
      .detail-track > div:nth-child(5) { display: none; }
      .play-layout { grid-template-columns: 1fr; gap: 22px; }
      .play-page { padding-top: 50px; }
      .vinyl-wrap { width: min(250px, 58vw, 34vh); }
      .lyric-scroll {
        --lyric-center-pad: 86px;
        height: min(36vh, 320px);
        gap: 16px;
      }
      .lyrics-line { display: none; }
    }
    @media (max-height: 760px) {
      :root {
        --player-row: 78px;
        --page-pad-y: 12px;
        --panel-pad: 12px;
        --panel-gap: 10px;
      }
      #homeView {
        grid-template-rows: minmax(124px, .5fr) minmax(0, 1.5fr);
      }
      #homeView > .section:first-child {
        padding: 11px 12px 10px;
      }
      .section-head {
        margin-bottom: 9px;
      }
      .rec-title {
        min-height: 34px;
        padding: 8px 7px 6px;
        font-size: 11px;
      }
      .track-cover {
        width: 36px;
        height: 36px;
      }
      .charts .track {
        min-height: 50px;
      }
      .detail-track {
        min-height: 46px;
      }
      .play-layout {
        gap: clamp(24px, 4.5vw, 70px);
      }
      .vinyl-wrap {
        width: min(clamp(240px, 35vh, 380px), 36vw, calc(100% - 24px));
      }
      .play-title {
        font-size: clamp(22px, 3.2vh, 34px);
      }
      .lyric-scroll {
        --lyric-center-pad: 104px;
        margin-top: 18px;
        height: clamp(230px, 39vh, 390px);
        gap: 18px;
      }
    }
    @media (max-width: 560px) {
      .vinyl-wrap { width: min(250px, 58vw, 34vh); }
    }
  </style>
</head>
<body>
  <main class="shell">
    <aside>
      <div class="brand"><div class="brand-dot">♪</div><span>NetEase</span></div>
      <div>
        <div class="nav-title">音乐库</div>
        <div class="nav-list">
          <div class="nav-item active">⌂ 发现音乐</div>
          <div class="nav-item">♬ 我的歌单</div>
          <div class="nav-item">♡ 我喜欢的音乐</div>
          <div class="nav-item">◷ 最近播放</div>
        </div>
      </div>
      <div>
        <div class="nav-title">收藏歌单</div>
        <div class="mini-playlists" id="sideRecs"></div>
      </div>
    </aside>
    <section class="content">
      <div class="topbar">
        <div class="search">
          <input id="keyword" placeholder="搜索歌曲、歌手、专辑..." />
          <button class="icon-btn" id="searchButton" title="搜索" aria-label="搜索">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="11" cy="11" r="7"></circle>
              <path d="m16 16 4 4"></path>
            </svg>
          </button>
        </div>
      </div>
        <div class="hero">
        <div id="homeView">
          <section class="section">
            <div class="section-head">
              <h2>收藏歌单</h2>
              <div class="pager-actions">
                <button class="see" id="prevRecPage">← 上一页</button>
                <button class="see" id="nextRecPage">下一页 →</button>
              </div>
          </div>
          <div class="recs recs-wide" id="recs"></div>
        </section>
        <div class="grid">
          <section class="section">
            <div class="section-head">
              <h2>创建歌单</h2>
            </div>
            <div class="playlist-strip playlist-stack" id="playlists"></div>
          </section>
          <section class="section">
            <div class="section-head">
              <h2>我喜欢的音乐</h2>
              <span class="see" id="likedCount"></span>
            </div>
            <div class="charts" id="charts"></div>
          </section>
        </div>
        </div>
        <section class="search-view hidden" id="searchView">
          <button class="see back-home" id="backSearchHome">← 返回首页</button>
          <section class="section">
            <div class="section-head">
              <div>
                <h2 id="searchTitle">搜索结果</h2>
                <div class="detail-meta" id="searchMeta"></div>
              </div>
            </div>
            <div class="detail-table-head">
              <span>#</span><span></span><span>标题</span><span>专辑</span><span>时长</span><span></span>
            </div>
            <div class="search-results" id="searchResults"></div>
          </section>
        </section>
        <section class="playlist-detail hidden" id="playlistView">
          <button class="see back-home" id="backHome">← 返回首页</button>
          <div class="detail-hero">
            <div id="detailCover"></div>
            <div>
              <div class="detail-kicker">歌单</div>
              <h1 class="detail-title" id="detailTitle">歌单</h1>
              <p class="detail-desc" id="detailDesc"></p>
              <div class="detail-meta" id="detailMeta"></div>
              <div class="detail-actions">
                <button class="primary-btn" id="playAll">▶ 播放全部</button>
                <button class="soft-btn" id="detailCount">歌曲</button>
              </div>
            </div>
          </div>
          <section class="section">
            <div class="detail-table-head">
              <span>#</span><span></span><span>标题</span><span>专辑</span><span>时长</span><span></span>
            </div>
            <div class="detail-tracks" id="detailTracks"></div>
          </section>
        </section>
        <section class="play-page hidden" id="playView">
          <button class="play-back" id="backFromPlay" title="收起播放页" aria-label="收起播放页">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
          </button>
          <div class="play-layout">
            <div class="vinyl-wrap" id="vinylWrap">
              <span class="vinyl-halo inner" aria-hidden="true"></span>
              <span class="vinyl-halo outer" aria-hidden="true"></span>
              <img class="play-cover" id="playCover" alt="" />
            </div>
            <div>
              <h1 class="play-title" id="playTitle">未播放</h1>
              <div class="play-sub" id="playSub">等待点歌</div>
              <div class="lyric-scroll" id="playLyrics"></div>
            </div>
          </div>
        </section>
      </div>
    </section>
    <footer class="player">
      <div class="player-timeline" id="playerTimeline" title="跳转播放进度">
        <div class="player-timeline-fill" id="playerTimelineFill"></div>
        <div class="player-timeline-tip" id="playerTimelineTip">00:00 / --:--</div>
      </div>
      <div class="now-playing">
        <img id="nowCover" alt="" />
        <div>
          <strong id="song">未播放</strong>
          <span id="meta">等待点歌</span>
        </div>
      </div>
      <div class="controls">
        <button class="icon-btn mode-control active" id="sequenceMode" title="顺序播放" aria-label="顺序播放">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 1l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="M7 23l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
        </button>
        <button class="icon-btn skip-icon" id="prevTrack" title="上一首" aria-label="上一首">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 20 9 12l10-8v16z"/><path d="M5 19V5"/></svg>
        </button>
        <button class="icon-btn primary-control" id="pause" title="暂停/继续" aria-label="暂停/继续">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.25 6.5v11l8.5-5.5z"/></svg>
        </button>
        <button class="icon-btn skip-icon" id="nextTrack" title="下一首" aria-label="下一首">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4l10 8-10 8V4z"/><path d="M19 5v14"/></svg>
        </button>
        <button class="icon-btn list-icon mode-control" id="shuffleMode" title="随机播放" aria-label="随机播放">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 3h5v5"/><path d="M4 20 21 3"/><path d="M21 16v5h-5"/><path d="M15 15l6 6"/><path d="M4 4l5 5"/></svg>
        </button>
      </div>
      <div class="progress-wrap">
        <button class="queue-toggle-mini" id="queueToggle" title="播放列表" aria-label="播放列表">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6h12"/><path d="M8 12h12"/><path d="M8 18h12"/><path d="M4 6h.01"/><path d="M4 12h.01"/><path d="M4 18h.01"/></svg>
        </button>
        <span><span id="status">00:00</span> / <span id="duration">--:--</span></span>
        <span class="volume-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M11 5 6 9H3v6h3l5 4V5z"/><path d="M16 9.5a4 4 0 0 1 0 5"/><path d="M19 7a8 8 0 0 1 0 10"/></svg>
        </span>
        <input class="volume-slider" id="volumeSlider" type="range" min="0" max="100" value="80" title="音量" aria-label="音量" />
      </div>
      <div class="wave" id="wave"></div>
      <div class="lyrics-line hidden" id="lyricLine"></div>
      <div class="queue-panel hidden" id="queuePanel">
        <div class="queue-head">
          <strong id="queueTitle">播放列表</strong>
          <span id="queueCount">0 首</span>
        </div>
        <div class="queue-items" id="queueItems"></div>
      </div>
    </footer>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const webClientToken = (crypto?.randomUUID?.() || String(Date.now()) + Math.random().toString(16).slice(2));
    let dashboard = { playlists: [], recommended_playlists: [], liked_tracks: [] };
    let currentPlaylist = null;
    let currentPlaylistTracks = [];
    let currentContext = null;
    let lastSearchTracks = [];
    let playbackQueue = [];
    let queueSource = "播放列表";
    let queueIndex = -1;
    let autoAdvanceInFlight = false;
    let autoAdvanceArmed = true;
    let autoAdvanceCooldownUntil = 0;
    let playMode = "sequence";
    let activeView = "home";
    let recPage = 0;
    let playlistOffset = 0;
    let playlistTotal = 0;
    let playlistHasMore = false;
    let playlistLoadingMore = false;
    let recommendedOffset = 0;
    let recommendedTotal = 0;
    let recommendedHasMore = false;
    let recommendedLoadingMore = false;
    let likedOffset = 0;
    let likedTotal = 0;
    let likedHasMore = false;
    let likedLoadingMore = false;
    let playbackPosition = 0;
    let playbackDuration = 0;
    let playbackActive = false;
    let playbackPaused = false;
    let playbackProgressPending = false;
    let playbackSyncedAt = Date.now();
    let playbackStartGuard = null;
    let playbackControlPendingUntil = 0;
    let playbackControlGuard = null;
    let playbackSeekGuard = null;
    let playbackReplayGuard = null;
    let activeLyricIndex = -1;
    let renderedLyricsKey = "";
    let playCloseTimer = null;
    let playerOverlayOpen = false;
    let lyricSyncInFlight = false;
    let timelineHovering = false;
    let timelineHoverRatio = 0;
    let playbackVolume = 80;
    let volumeUserControlled = false;
    let volumeSyncInFlight = false;
    let volumeQueuedValue = null;
    let volumeApplyTimer = null;
    let recPageSize = 7;
    const lyricLeadSeconds = 2.85;
    const playIconSvg = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.25 6.5v11l8.5-5.5z"/></svg>';
    const pauseIconSvg = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>';

    async function api(path, body) {
      const res = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: body ? { "content-type": "application/json", "x-web-player-client": webClientToken } : { "x-web-player-client": webClientToken },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json();
      if (!res.ok || data.success === false) throw new Error(data.message || data.error || "请求失败");
      return data;
    }
    function escapeHtml(text) {
      return String(text ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    function image(url, cls) {
      if (!url) return '<div class="' + cls + ' fallback"></div>';
      return '<img class="' + cls + '" src="' + escapeHtml(url) + '" alt="" loading="eager" decoding="async" referrerpolicy="no-referrer" />';
    }
    function updateResponsiveMetrics(options = {}) {
      const recs = $("recs");
      if (!recs) return;
      const width = recs.clientWidth || window.innerWidth || 980;
      const height = recs.clientHeight || 160;
      let minCardWidth = 118;
      if (width < 980) minCardWidth = 104;
      if (width < 720) minCardWidth = 88;
      if (height < 140) minCardWidth = Math.max(82, minCardWidth - 10);
      const nextSize = Math.max(3, Math.min(8, Math.floor((width + 9) / (minCardWidth + 9)) || 7));
      document.documentElement.style.setProperty("--rec-columns", String(nextSize));
      if (nextSize !== recPageSize) {
        const currentStart = recPage * recPageSize;
        recPageSize = nextSize;
        recPage = Math.max(0, Math.floor(currentStart / recPageSize));
        if (options.rerender) renderRecommended();
      }
    }
    function durationText(ms) {
      const n = Number(ms || 0);
      if (!n) return "--:--";
      const total = Math.round(n / 1000);
      const minutes = Math.floor(total / 60);
      const seconds = String(total % 60).padStart(2, "0");
      return minutes + ":" + seconds;
    }
    function trackDurationMs(track) {
      return Number(track?.duration ?? track?.durationMs ?? 0);
    }
    function clockText(seconds) {
      const n = Math.max(0, Math.round(Number(seconds || 0)));
      const minutes = Math.floor(n / 60);
      const rest = String(n % 60).padStart(2, "0");
      return minutes + ":" + rest;
    }
    function setPlaybackButtonIcon(active = playbackActive, paused = playbackPaused) {
      const button = $("pause");
      if (!button) return;
      const isPlaying = Boolean(active && !paused);
      button.innerHTML = isPlaying ? pauseIconSvg : playIconSvg;
      button.classList.toggle("is-playing", isPlaying);
      button.title = isPlaying ? "暂停" : "播放";
      button.setAttribute("aria-label", isPlaying ? "暂停" : "播放");
    }
    function optimisticTogglePlaybackButton() {
      const hasPlayback = Boolean(currentContext?.playback?.id || currentContext?.playback?.name || playbackActive);
      if (!hasPlayback) return false;
      const nextPaused = playbackActive && !playbackPaused;
      if (nextPaused) {
        playbackPosition = computedPlaybackPosition();
      }
      playbackActive = true;
      playbackPaused = nextPaused;
      playbackSyncedAt = Date.now();
      playbackControlPendingUntil = Date.now() + 6000;
      playbackControlGuard = {
        trackId: currentContext?.playback?.id ? String(currentContext.playback.id) : "",
        active: true,
        paused: nextPaused,
        expiresAt: playbackControlPendingUntil,
      };
      currentContext = {
        ...(currentContext || {}),
        active: playbackActive,
        paused: playbackPaused,
        position: playbackPosition,
        duration: playbackDuration,
      };
      setPlaybackButtonIcon(playbackActive, playbackPaused);
      updateProgressUi(playbackPosition, playbackDuration);
      if (playerOverlayOpen) renderPlayerPage(currentContext);
      return true;
    }
    function normalizeVolumeValue(volume) {
      const raw = Number(volume);
      if (!Number.isFinite(raw)) return null;
      const scaled = raw > 0 && raw <= 1 ? raw * 100 : raw;
      return Math.max(0, Math.min(100, Math.round(scaled)));
    }
    function updateVolumeUi(volume = playbackVolume) {
      const slider = $("volumeSlider");
      if (!slider) return;
      const value = normalizeVolumeValue(volume);
      if (value === null) return;
      playbackVolume = value;
      slider.value = String(value);
      slider.style.setProperty("--volume", value + "%");
      slider.title = "音量 " + value + "%";
      slider.setAttribute("aria-valuetext", value + "%");
    }
    async function applyPlayerVolume(volume = playbackVolume) {
      const value = normalizeVolumeValue(volume);
      if (value === null) return;
      if (volumeSyncInFlight) {
        volumeQueuedValue = value;
        return;
      }
      volumeSyncInFlight = true;
      try {
        await api("/api/volume", { volume: value });
      } finally {
        volumeSyncInFlight = false;
        if (volumeQueuedValue !== null && volumeQueuedValue !== value) {
          const nextValue = volumeQueuedValue;
          volumeQueuedValue = null;
          void applyPlayerVolume(nextValue);
        } else {
          volumeQueuedValue = null;
        }
      }
    }
    function schedulePlayerVolume(volume = playbackVolume) {
      const value = normalizeVolumeValue(volume);
      if (value === null) return;
      if (volumeApplyTimer) clearTimeout(volumeApplyTimer);
      volumeApplyTimer = setTimeout(() => {
        volumeApplyTimer = null;
        void applyPlayerVolume(value).catch((error) => {
          $("status").textContent = error.message || "音量设置失败";
        });
      }, 90);
    }
    function updatePlayModeUi() {
      $("sequenceMode").classList.toggle("active", playMode === "sequence");
      $("shuffleMode").classList.toggle("active", playMode === "shuffle");
    }
    function setPlayMode(mode) {
      playMode = mode === "shuffle" ? "shuffle" : "sequence";
      updatePlayModeUi();
    }
    function updateProgressUi(position = playbackPosition, duration = playbackDuration) {
      const safeDuration = Number(duration || 0);
      const safePosition = safeDuration > 0 ? Math.max(0, Math.min(safeDuration, Number(position || 0))) : 0;
      const percent = safeDuration > 0 ? Math.max(0, Math.min(100, (safePosition / safeDuration) * 100)) : 0;
      $("playerTimelineFill").style.setProperty("--progress", percent + "%");
      $("status").textContent = clockText(safePosition);
      $("duration").textContent = safeDuration ? clockText(safeDuration) : "--:--";
      if (!timelineHovering) {
        $("playerTimelineTip").textContent = clockText(safePosition) + " / " + (safeDuration ? clockText(safeDuration) : "--:--");
      }
    }
    function armReplayGuard(id, ms = 8000) {
      playbackReplayGuard = {
        trackId: id ? String(id) : "",
        expiresAt: Date.now() + ms,
      };
    }
    function updateTimelineHover(event) {
      const rect = $("playerTimeline").getBoundingClientRect();
      timelineHoverRatio = rect.width ? Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) : 0;
      timelineHovering = true;
      $("playerTimeline").style.setProperty("--hover-x", (timelineHoverRatio * 100) + "%");
      const hoverSeconds = playbackDuration ? timelineHoverRatio * playbackDuration : computedPlaybackPosition();
      $("playerTimelineTip").textContent = clockText(hoverSeconds);
    }
    function computedPlaybackPosition() {
      if (playbackProgressPending) return playbackPosition;
      if (!playbackActive || playbackPaused || !playbackDuration) return playbackPosition;
      const elapsed = (Date.now() - playbackSyncedAt) / 1000;
      return Math.min(playbackDuration, playbackPosition + elapsed);
    }
    function updateVinylWave() {
      const wrap = $("vinylWrap");
      if (!wrap) return;
      if (!playbackActive || playbackPaused) {
        wrap.style.setProperty("--wave-beat", "0");
        wrap.style.setProperty("--wave-soft", "0");
        return;
      }
      const t = performance.now() / 1000;
      const position = computedPlaybackPosition();
      const fast = Math.max(0, Math.sin(t * 5.4 + position * .08));
      const mid = Math.max(0, Math.sin(t * 2.9 + position * .15 + 1.4));
      const flutter = Math.max(0, Math.sin(t * 9.8 + position * .03));
      const beat = Math.min(1, Math.pow(fast * .58 + mid * .32 + flutter * .18, 1.35));
      const soft = Math.min(1, beat * .58 + mid * .42);
      wrap.style.setProperty("--wave-beat", beat.toFixed(3));
      wrap.style.setProperty("--wave-soft", soft.toFixed(3));
    }
    function findActiveLyricIndex(lyrics, position) {
      if (!lyrics.length) return -1;
      let active = lyrics.findIndex((line, index) => {
        const next = lyrics[index + 1];
        return line.time <= position && (!next || next.time > position);
      });
      if (active < 0) active = 0;
      return active;
    }
    function centerLyricRow(row) {
      const container = $("playLyrics");
      if (!container || !row) return;
      const centerPad = Math.max(0, Math.round((container.clientHeight - row.clientHeight) / 2));
      container.style.setProperty("--lyric-center-pad", centerPad + "px");
      requestAnimationFrame(() => {
        const containerRect = container.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        const rowCenterInScrollContent = container.scrollTop + (rowRect.top - containerRect.top) + rowRect.height / 2;
        const target = Math.max(0, Math.min(rowCenterInScrollContent - container.clientHeight / 2, container.scrollHeight - container.clientHeight));
        if (Math.abs(container.scrollTop - target) > 8) {
          container.scrollTo({ top: target, behavior: "smooth" });
        }
      });
    }
    function updateLyricHighlight(position = computedPlaybackPosition()) {
      if (!playerOverlayOpen) return;
      const lyrics = currentContext?.lyrics || [];
      const index = findActiveLyricIndex(lyrics, Number(position || 0) + lyricLeadSeconds);
      if (index === activeLyricIndex) return;
      const rows = $("playLyrics").querySelectorAll(".lyric-row");
      if (!rows.length) return;
      if (index >= 0 && rows[index]) {
        rows[index].classList.add("active");
        centerLyricRow(rows[index]);
      }
      if (activeLyricIndex >= 0 && activeLyricIndex !== index && rows[activeLyricIndex]) {
        rows[activeLyricIndex].classList.remove("active");
      }
      activeLyricIndex = index;
    }
    function lyricsKey(data) {
      const playbackId = data?.playback?.id || "";
      const lyrics = data?.lyrics || [];
      return playbackId + ":" + lyrics.length + ":" + (lyrics[0]?.time ?? "") + ":" + (lyrics.at(-1)?.time ?? "");
    }
    function updateLocalProgress() {
      const localPosition = computedPlaybackPosition();
      const replayGuardActive = Boolean(playbackReplayGuard && Date.now() < playbackReplayGuard.expiresAt);
      updateProgressUi(localPosition, playbackDuration);
      updateVinylWave();
      updateLyricHighlight(localPosition);
      if (!replayGuardActive && !playbackProgressPending && playbackDuration && playbackActive && !playbackPaused && localPosition < playbackDuration - 1) {
        autoAdvanceArmed = true;
      }
      if (!replayGuardActive && !playbackProgressPending && autoAdvanceArmed && Date.now() >= autoAdvanceCooldownUntil && playbackDuration && playbackActive && !playbackPaused && localPosition >= playbackDuration - .35) {
        autoAdvanceArmed = false;
        autoAdvanceCooldownUntil = Date.now() + 5000;
        $("nextTrack").click();
      }
      requestAnimationFrame(updateLocalProgress);
    }
    function showView(name) {
      activeView = name;
      $("homeView").classList.toggle("hidden", name !== "home");
      $("searchView").classList.toggle("hidden", name !== "search");
      $("playlistView").classList.toggle("hidden", name !== "playlist");
      document.querySelector(".hero").scrollTop = 0;
    }
    function collapsePlayerPage() {
      const playView = $("playView");
      if (!playView || playView.classList.contains("closing")) return;
      playView.classList.remove("opening");
      playView.classList.add("closing");
      playerOverlayOpen = false;
      clearTimeout(playCloseTimer);
      playCloseTimer = setTimeout(() => {
        playView.classList.add("hidden");
        playView.classList.remove("closing");
      }, 340);
    }
    function firstPlayableTrack() {
      return currentPlaylistTracks.find((track) => track.id);
    }
    function setPlaybackQueue(source, tracks, startIndex = 0) {
      playbackQueue = (tracks || []).filter((track) => track?.id).map((track) => ({
        id: String(track.id),
        name: track.name || "未知歌曲",
        artist: track.artist || "",
        album: track.album || "",
        coverUrl: track.coverUrl || "",
        duration: trackDurationMs(track),
      }));
      queueSource = source || "播放列表";
      queueIndex = playbackQueue.length ? Math.max(0, Math.min(playbackQueue.length - 1, Number(startIndex || 0))) : -1;
      autoAdvanceArmed = true;
      autoAdvanceCooldownUntil = 0;
      renderQueue();
    }
    function renderQueue() {
      $("queueTitle").textContent = queueSource || "播放列表";
      $("queueCount").textContent = playbackQueue.length ? String(playbackQueue.length) + " 首" : "0 首";
      $("queueItems").innerHTML = playbackQueue.length ? playbackQueue.map((track, index) => (
        '<button class="queue-item ' + (index === queueIndex ? "active" : "") + '" data-queue-index="' + index + '">' +
          '<span class="queue-index">' + String(index + 1).padStart(2, "0") + '</span>' +
          '<span class="queue-main"><strong>' + escapeHtml(track.name) + '</strong><span>' + escapeHtml(track.artist || track.album || "") + '</span></span>' +
          '<span class="queue-time">' + durationText(track.duration) + '</span>' +
        '</button>'
      )).join("") : '<div class="empty">暂无播放队列</div>';
    }
    async function playQueueIndex(index, { replayCurrent = false } = {}) {
      if (!playbackQueue.length) return;
      const nextIndex = Math.max(0, Math.min(playbackQueue.length - 1, Number(index || 0)));
      const track = playbackQueue[nextIndex];
      if (!track?.id) return;
      queueIndex = nextIndex;
      autoAdvanceCooldownUntil = Date.now() + 5000;
      renderQueue();
      const replayingSameTrack = replayCurrent;
      if (replayingSameTrack) {
        await replayTrack(track.id, { optimisticTrack: track });
      } else {
        await playTrack(track.id, { optimisticTrack: track });
      }
    }
    function syncQueueIndexFromCurrent() {
      if (queueIndex >= 0) return queueIndex;
      const currentId = currentContext?.playback?.id;
      if (!currentId) return queueIndex;
      const found = playbackQueue.findIndex((track) => String(track.id) === String(currentId));
      if (found >= 0) {
        queueIndex = found;
        renderQueue();
      }
      return queueIndex;
    }
    async function playPreviousFromQueue() {
      if (autoAdvanceInFlight || !playbackQueue.length) return;
      syncQueueIndexFromCurrent();
      if (queueIndex < 0) return;
      const targetIndex = computedPlaybackPosition() > 3 ? queueIndex : Math.max(0, queueIndex - 1);
      await playQueueIndex(targetIndex);
    }
    async function playNextFromQueue() {
      syncQueueIndexFromCurrent();
      if (autoAdvanceInFlight) return;
      autoAdvanceInFlight = true;
      autoAdvanceArmed = false;
      autoAdvanceCooldownUntil = Date.now() + 5000;
      try {
        if (!playbackQueue.length || queueIndex < 0) {
          const currentId = currentContext?.playback?.id;
          if (currentId) {
            await replayTrack(currentId, { optimisticTrack: currentContext.playback });
          }
          return;
        }
        let nextIndex = queueIndex + 1;
        if (playMode === "shuffle") {
          if (playbackQueue.length === 1) nextIndex = 0;
          else {
            do {
              nextIndex = Math.floor(Math.random() * playbackQueue.length);
            } while (nextIndex === queueIndex);
          }
        } else if (queueIndex >= playbackQueue.length - 1) {
          nextIndex = queueIndex;
        }
        await playQueueIndex(nextIndex, { replayCurrent: nextIndex === queueIndex });
      } finally {
        autoAdvanceInFlight = false;
      }
    }
    function renderPlaylists() {
      const items = dashboard.playlists || [];
      if (items.length === 0) {
        $("playlists").innerHTML = '<div class="empty">暂无歌单</div>';
        return;
      }
      $("playlists").innerHTML = items.map((p) => (
        '<button class="playlist-card" data-playlist="' + escapeHtml(p.id) + '">' +
          image(p.coverUrl, "cover") +
          '<strong>' + escapeHtml(p.name) + '</strong>' +
          '<span>' + escapeHtml(String(p.trackCount || 0)) + ' songs</span>' +
        '</button>'
      )).join("") + (playlistHasMore ? '<div class="empty playlist-loading">继续下滑加载更多歌单...</div>' : "");
    }
    async function loadMorePlaylists() {
      if (playlistLoadingMore || !playlistHasMore) return;
      playlistLoadingMore = true;
      try {
        const data = await api("/api/playlists?offset=" + encodeURIComponent(String(playlistOffset)) + "&limit=24");
        const seen = new Set((dashboard.playlists || []).map((playlist) => String(playlist.id)));
        const incoming = (data.playlists || []).filter((playlist) => !seen.has(String(playlist.id)));
        dashboard.playlists = [...(dashboard.playlists || []), ...incoming];
        playlistOffset = dashboard.playlists.length;
        playlistTotal = Number(data.total || playlistTotal || playlistOffset);
        playlistHasMore = Boolean(data.hasMore && playlistOffset < playlistTotal);
        renderPlaylists();
      } catch (error) {
        $("status").textContent = error.message || "加载歌单失败";
      } finally {
        playlistLoadingMore = false;
      }
    }
    function maybeLoadMorePlaylists() {
      const el = $("playlists");
      if (!el || playlistLoadingMore || !playlistHasMore) return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 96) {
        void loadMorePlaylists();
      }
    }
    function renderRecommended() {
      updateResponsiveMetrics();
      const all = dashboard.recommended_playlists || [];
      const totalPages = Math.max(1, Math.ceil(all.length / recPageSize));
      recPage = ((recPage % totalPages) + totalPages) % totalPages;
      const start = recPage * recPageSize;
      const items = all.slice(start, start + recPageSize);
      $("recs").innerHTML = items.length ? items.map((p) => (
        '<button class="rec" data-playlist="' + escapeHtml(p.id) + '">' +
          image(p.coverUrl, "rec-cover") +
          '<span class="rec-count">♪ ' + escapeHtml(String(p.trackCount || 0)) + '首</span>' +
          '<span class="rec-title">' + escapeHtml(p.name) + '</span>' +
        '</button>'
      )).join("") : '<div class="empty">暂无收藏歌单</div>';
      $("sideRecs").innerHTML = all.slice(0, 5).map((p) => (
        '<div class="mini">' + image(p.coverUrl, "") +
        '<div><strong>' + escapeHtml(p.name) + '</strong><span>' + escapeHtml(p.creator || "") + '</span></div></div>'
      )).join("");
      applyRecCardColors();
    }
    function setRecommendedPagerLoading(isLoading) {
      $("prevRecPage").disabled = Boolean(isLoading);
      $("nextRecPage").disabled = Boolean(isLoading);
      if (isLoading) {
        $("recs").innerHTML = '<div class="empty rec-page-loading">加载中...</div>';
      }
    }
    function preloadRecommendedCovers(items) {
      const covers = items.map((item) => item.coverUrl).filter(Boolean);
      if (!covers.length) return Promise.resolve();
      return Promise.all(covers.map((src) => new Promise((resolve) => {
        const img = new Image();
        const done = () => resolve();
        const timer = setTimeout(done, 2200);
        img.onload = () => { clearTimeout(timer); done(); };
        img.onerror = () => { clearTimeout(timer); done(); };
        img.referrerPolicy = "no-referrer";
        img.src = src;
      })));
    }
    async function loadMoreRecommendedPlaylists(minCount = recPageSize) {
      if (recommendedLoadingMore || !recommendedHasMore) return;
      recommendedLoadingMore = true;
      try {
        const data = await api("/api/recommended-playlists?offset=" + encodeURIComponent(String(recommendedOffset)) + "&limit=" + encodeURIComponent(String(Math.max(14, minCount))));
        const seen = new Set((dashboard.recommended_playlists || []).map((playlist) => String(playlist.id)));
        const incoming = (data.playlists || []).filter((playlist) => !seen.has(String(playlist.id)));
        dashboard.recommended_playlists = [...(dashboard.recommended_playlists || []), ...incoming];
        recommendedOffset = dashboard.recommended_playlists.length;
        recommendedTotal = Number(data.total || recommendedTotal || recommendedOffset);
        recommendedHasMore = Boolean(data.hasMore && recommendedOffset < recommendedTotal);
      } catch (error) {
        $("status").textContent = error.message || "加载收藏歌单失败";
      } finally {
        recommendedLoadingMore = false;
      }
    }
    async function nextRecommendedPage() {
      if (recommendedLoadingMore) return;
      const nextPage = recPage + 1;
      const needed = (nextPage + 1) * recPageSize;
      setRecommendedPagerLoading(true);
      if (needed > (dashboard.recommended_playlists || []).length && recommendedHasMore) {
        await loadMoreRecommendedPlaylists(needed - (dashboard.recommended_playlists || []).length);
      }
      const totalPages = Math.max(1, Math.ceil((dashboard.recommended_playlists || []).length / recPageSize));
      recPage = nextPage >= totalPages ? 0 : nextPage;
      const start = recPage * recPageSize;
      await preloadRecommendedCovers((dashboard.recommended_playlists || []).slice(start, start + recPageSize));
      renderRecommended();
      setRecommendedPagerLoading(false);
    }
    function applyRecCardColors() {
      document.querySelectorAll(".rec").forEach((card) => {
        const cover = card.querySelector(".rec-cover");
        if (!cover?.src) return;
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => {
          try {
            const canvas = document.createElement("canvas");
            const size = 24;
            canvas.width = size;
            canvas.height = size;
            const ctx = canvas.getContext("2d", { willReadFrequently: true });
            ctx.drawImage(img, 0, 0, size, size);
            const startY = Math.floor(size * .54);
            const data = ctx.getImageData(0, startY, size, size - startY).data;
            let r = 0, g = 0, b = 0, count = 0;
            for (let i = 0; i < data.length; i += 4) {
              if (data[i + 3] < 20) continue;
              r += data[i];
              g += data[i + 1];
              b += data[i + 2];
              count += 1;
            }
            if (!count) return;
            const rgb = [
              Math.round(r / count),
              Math.round(g / count),
              Math.round(b / count),
            ].join(", ");
            card.style.setProperty("--rec-rgb", rgb);
          } catch {}
        };
        img.src = cover.src;
      });
    }
    function renderCharts() {
      const tracks = dashboard.liked_tracks || [];
      $("likedCount").textContent = dashboard.liked_total ? String(dashboard.liked_total) + " liked" : "";
      $("charts").innerHTML = tracks.length ? tracks.map((track, index) => (
        '<div class="track">' +
          '<div class="rank">' + String(index + 1).padStart(2, "0") + '</div>' +
          image(track.coverUrl, "track-cover") +
          '<div class="track-main"><strong>' + escapeHtml(track.name) + '</strong><span>' + escapeHtml(track.album || "") + '</span></div>' +
          '<div class="track-artist">' + escapeHtml(track.artist || "") + '</div>' +
          '<div class="track-time">' + durationText(track.duration) + '</div>' +
          '<button class="play-small" data-track="' + escapeHtml(track.id) + '" data-liked-index="' + index + '" title="播放">▶</button>' +
          '<div class="more">⋮</div>' +
        '</div>'
      )).join("") + (likedHasMore ? '<div class="empty liked-loading">继续下滑加载更多歌曲...</div>' : "") : '<div class="empty">暂无喜欢的音乐</div>';
    }
    async function loadMoreLikedTracks() {
      if (likedLoadingMore || !likedHasMore) return;
      likedLoadingMore = true;
      try {
        const data = await api("/api/liked-tracks?offset=" + encodeURIComponent(String(likedOffset)) + "&limit=24");
        const seen = new Set((dashboard.liked_tracks || []).map((track) => String(track.id)));
        const incoming = (data.tracks || []).filter((track) => !seen.has(String(track.id)));
        dashboard.liked_tracks = [...(dashboard.liked_tracks || []), ...incoming];
        likedOffset = dashboard.liked_tracks.length;
        likedTotal = Number(data.total || likedTotal || likedOffset);
        likedHasMore = Boolean(data.hasMore && likedOffset < likedTotal);
        renderCharts();
      } catch (error) {
        $("status").textContent = error.message || "加载喜欢的音乐失败";
      } finally {
        likedLoadingMore = false;
      }
    }
    function maybeLoadMoreLikedTracks() {
      const el = $("charts");
      if (!el || likedLoadingMore || !likedHasMore) return;
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 90) {
        void loadMoreLikedTracks();
      }
    }
    function renderPlaylistDetail(data) {
      currentPlaylist = data.playlist;
      currentPlaylistTracks = data.tracks || [];
      $("detailCover").innerHTML = image(currentPlaylist.coverUrl, "detail-cover");
      $("detailTitle").textContent = currentPlaylist.name || "歌单";
      $("detailDesc").textContent = currentPlaylist.description || "来自网易云音乐的歌单";
      $("detailMeta").textContent = [currentPlaylist.creator, String(currentPlaylist.trackCount || currentPlaylistTracks.length) + " 首歌"].filter(Boolean).join(" · ");
      $("detailCount").textContent = String(currentPlaylist.trackCount || currentPlaylistTracks.length) + " 首";
      $("detailTracks").innerHTML = currentPlaylistTracks.length ? currentPlaylistTracks.map((track, index) => (
        '<div class="detail-track">' +
          '<div class="rank">' + String(index + 1).padStart(2, "0") + '</div>' +
          image(track.coverUrl, "track-cover") +
          '<div class="track-main"><strong>' + escapeHtml(track.name) + '</strong><span>' + escapeHtml(track.artist || "") + '</span></div>' +
          '<div class="track-artist">' + escapeHtml(track.album || "") + '</div>' +
          '<div class="track-time">' + durationText(track.duration) + '</div>' +
          '<button class="play-small" data-track="' + escapeHtml(track.id) + '" data-playlist-index="' + index + '" title="播放">▶</button>' +
        '</div>'
      )).join("") : '<div class="empty">这个歌单里暂时没有可显示的歌曲</div>';
    }
    function renderSearchResults(data) {
      const tracks = data.tracks || [];
      lastSearchTracks = tracks;
      const keyword = data.keyword || $("keyword").value.trim();
      $("searchTitle").textContent = keyword ? "搜索：" + keyword : "搜索结果";
      $("searchMeta").textContent = tracks.length ? String(tracks.length) + " 首结果" : "没有找到相关歌曲";
      $("searchResults").innerHTML = tracks.length ? tracks.map((track, index) => (
        '<div class="detail-track">' +
          '<div class="rank">' + String(index + 1).padStart(2, "0") + '</div>' +
          image(track.coverUrl, "track-cover") +
          '<div class="track-main"><strong>' + escapeHtml(track.name) + '</strong><span>' + escapeHtml(track.artist || "") + '</span></div>' +
          '<div class="track-artist">' + escapeHtml(track.album || "") + '</div>' +
          '<div class="track-time">' + durationText(track.duration ?? track.durationMs) + '</div>' +
          '<button class="play-small" data-track="' + escapeHtml(track.id) + '" title="播放">▶</button>' +
        '</div>'
      )).join("") : '<div class="empty">没有找到相关歌曲</div>';
    }
    async function openPlaylist(id) {
      $("status").textContent = "读取歌单";
      $("detailTracks").innerHTML = '<div class="empty">正在加载歌单...</div>';
      showView("playlist");
      const data = await api("/api/playlist?id=" + encodeURIComponent(id));
      renderPlaylistDetail(data);
    }
    function renderPlayerPage(data) {
      const p = data?.playback || {};
      $("vinylWrap").classList.toggle("spinning", Boolean(data?.active && !data?.paused));
      $("playTitle").textContent = p.name || "未播放";
      $("playSub").textContent = p.artist || "等待点歌";
      if (p.coverUrl) {
        $("playCover").src = p.coverUrl;
        $("playView").style.setProperty("--player-cover", "url('" + p.coverUrl.replace(/'/g, "%27") + "')");
        applyCoverColor(p.coverUrl);
      }
      const lyrics = data?.lyrics || [];
      const key = lyricsKey(data);
      if (key !== renderedLyricsKey) {
        $("playLyrics").innerHTML = lyrics.length ? lyrics.map((line) => (
          '<div class="lyric-row">' +
            '<div class="lyric-original">' + escapeHtml(line.text || "") + '</div>' +
            (line.translation ? '<div class="lyric-translation">' + escapeHtml(line.translation) + '</div>' : "") +
          '</div>'
        )).join("") : '<div class="empty">暂无歌词</div>';
        renderedLyricsKey = key;
        activeLyricIndex = -1;
      }
      updateLyricHighlight(playerOverlayOpen ? computedPlaybackPosition() : Number(data?.position ?? 0));
    }
    function findKnownTrack(id) {
      const key = String(id);
      return [
        playbackQueue,
        currentPlaylistTracks,
        dashboard.liked_tracks || [],
        lastSearchTracks,
      ].flat().find((track) => String(track?.id) === key);
    }
    function optimisticPlayback(track, id) {
      const fallbackId = String(track?.id ?? id ?? "");
      const duration = trackDurationMs(track) / 1000;
      const playback = {
        id: fallbackId,
        name: track?.name || "正在播放",
        artist: track?.artist || "",
        album: track?.album || "",
        coverUrl: track?.coverUrl || "",
        durationMs: trackDurationMs(track) || null,
        style: "",
      };
      currentContext = {
        ...(currentContext || {}),
        success: true,
        active: true,
        paused: false,
        position: 0,
        duration,
        playback,
        lyrics: String(currentContext?.playback?.id) === fallbackId ? (currentContext?.lyrics || []) : [],
        current_lyrics: [],
      };
      playbackDuration = duration || playbackDuration;
      playbackPosition = 0;
      playbackActive = true;
      playbackPaused = false;
      playbackProgressPending = true;
      playbackStartGuard = {
        trackId: fallbackId,
        expiresAt: Date.now() + 20000,
      };
      playbackSyncedAt = Date.now();
      activeLyricIndex = -1;
      renderedLyricsKey = "";
      $("song").textContent = playback.name;
      $("meta").textContent = playback.artist || "正在播放";
      if (playback.coverUrl) {
        $("nowCover").src = playback.coverUrl;
        $("nowCover").style.display = "block";
      }
      setPlaybackButtonIcon(true, false);
      updateProgressUi(0, playbackDuration);
      if (playerOverlayOpen) renderPlayerPage(currentContext);
    }
    function openPlayerPage() {
      if (!currentContext?.playback?.name) return;
      const playView = $("playView");
      playerOverlayOpen = true;
      clearTimeout(playCloseTimer);
      playView.classList.remove("hidden", "closing");
      playView.classList.add("opening");
      renderPlayerPage(currentContext);
      syncLyricFromCli();
      setTimeout(() => playView.classList.remove("opening"), 360);
    }
    function applyCoverColor(url) {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        try {
          const canvas = document.createElement("canvas");
          const size = 24;
          canvas.width = size;
          canvas.height = size;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, size, size);
          const data = ctx.getImageData(0, 0, size, size).data;
          let r = 0, g = 0, b = 0, count = 0;
          for (let i = 0; i < data.length; i += 4) {
            const alpha = data[i + 3];
            if (alpha < 128) continue;
            r += data[i]; g += data[i + 1]; b += data[i + 2]; count++;
          }
          if (count) {
            const color = "rgb(" + Math.round(r / count) + "," + Math.round(g / count) + "," + Math.round(b / count) + ")";
            $("playView").style.setProperty("--player-color", color);
            document.documentElement.style.setProperty("--footer-color", color);
          }
        } catch {
          $("playView").style.setProperty("--player-color", "#111923");
          document.documentElement.style.setProperty("--footer-color", "#121923");
        }
      };
      img.onerror = () => {
        $("playView").style.setProperty("--player-color", "#111923");
        document.documentElement.style.setProperty("--footer-color", "#121923");
      };
      img.src = url;
    }
    async function loadDashboard() {
      $("charts").innerHTML = '<div class="empty">正在加载网易云音乐...</div>';
      dashboard = await api("/api/dashboard");
      playlistOffset = (dashboard.playlists || []).length;
      playlistTotal = Number(dashboard.playlist_total || playlistOffset);
      playlistHasMore = Boolean(dashboard.playlist_has_more && playlistOffset < playlistTotal);
      recommendedOffset = (dashboard.recommended_playlists || []).length;
      recommendedTotal = Number(dashboard.recommended_total || recommendedOffset);
      recommendedHasMore = Boolean(dashboard.recommended_has_more && recommendedOffset < recommendedTotal);
      likedOffset = (dashboard.liked_tracks || []).length;
      likedTotal = Number(dashboard.liked_total || likedOffset);
      likedHasMore = Boolean(dashboard.liked_has_more && likedOffset < likedTotal);
      recPage = 0;
      renderPlaylists();
      renderRecommended();
      renderCharts();
    }
    async function refresh({ allowExternalChange = false } = {}) {
      const data = await api("/api/context");
      const p = data.playback || {};
      const pendingId = currentContext?.playback?.id ? String(currentContext.playback.id) : "";
      const incomingId = p.id ? String(p.id) : "";
      const duration = Number(data?.status?.duration ?? data?.duration ?? 0);
      const position = Number(data?.position ?? 0);
      const contextPending = Boolean(data?.status?.pending);
      const hasPlayback = Boolean(p.id || p.name);
      const nextActive = Boolean(hasPlayback && data?.active);
      const nextPaused = hasPlayback ? Boolean(data?.paused) : true;
      const replayGuard = playbackReplayGuard;
      const sameReplayTrack = Boolean(replayGuard && (!replayGuard.trackId || !incomingId || replayGuard.trackId === incomingId));
      if (replayGuard && Date.now() >= replayGuard.expiresAt) {
        playbackReplayGuard = null;
      } else if (sameReplayTrack && position > 10) {
        return false;
      }
      if (playbackProgressPending && pendingId) {
        const startGuardActive = Boolean(
          playbackStartGuard &&
          playbackStartGuard.trackId === pendingId &&
          Date.now() < playbackStartGuard.expiresAt
        );
        const targetConfirmed = incomingId === pendingId && nextActive && !nextPaused && !contextPending;
        if (startGuardActive && !targetConfirmed) return false;
        playbackStartGuard = null;
        if (incomingId && incomingId !== pendingId) {
          return false;
        }
      }
      $("song").textContent = p.name || "未播放";
      $("meta").textContent = p.artist || "等待点歌";
      const wasPending = playbackProgressPending;
      const now = Date.now();
      const guard = playbackControlGuard;
      const sameGuardTrack = Boolean(guard && (!guard.trackId || !incomingId || guard.trackId === incomingId));
      const guardMatched = Boolean(guard && sameGuardTrack && nextActive === guard.active && nextPaused === guard.paused);
      const guardActive = Boolean(guard && sameGuardTrack && now < guard.expiresAt && !guardMatched);
      if (guardMatched || (guard && now >= guard.expiresAt)) {
        playbackControlGuard = null;
        playbackControlPendingUntil = 0;
      }
      const seekGuard = playbackSeekGuard;
      const sameSeekTrack = Boolean(seekGuard && (!seekGuard.trackId || !incomingId || seekGuard.trackId === incomingId));
      const seekMatched = Boolean(seekGuard && sameSeekTrack && Math.abs(position - seekGuard.position) <= 1.25);
      const seekGuardActive = Boolean(seekGuard && sameSeekTrack && now < seekGuard.expiresAt && !seekMatched);
      if (seekMatched || (seekGuard && now >= seekGuard.expiresAt)) {
        playbackSeekGuard = null;
      }
      const controlPending = guardActive || seekGuardActive || (now < playbackControlPendingUntil && (!pendingId || !incomingId || pendingId === incomingId));
      playbackDuration = duration || playbackDuration;
      if (controlPending) {
        playbackPosition = playbackPaused ? playbackPosition : computedPlaybackPosition();
      } else if (contextPending && incomingId === pendingId) {
        playbackPosition = 0;
        playbackActive = true;
        playbackPaused = false;
      } else if (wasPending && nextActive && !nextPaused) {
        playbackPosition = position;
        playbackActive = true;
        playbackPaused = false;
        playbackProgressPending = false;
        playbackStartGuard = null;
      } else if (wasPending && !nextActive) {
        playbackPosition = 0;
        playbackActive = true;
        playbackPaused = false;
      } else {
        playbackPosition = position;
        playbackActive = nextActive;
        playbackPaused = nextPaused;
      }
      playbackSyncedAt = Date.now();
      currentContext = {
        ...data,
        active: playbackActive,
        paused: playbackPaused,
        position: playbackPosition,
        duration: playbackDuration,
      };
      setPlaybackButtonIcon();
      if (!volumeUserControlled && Number.isFinite(Number(data?.status?.volume))) {
        updateVolumeUi(Number(data.status.volume));
      }
      updateProgressUi(playbackPosition, playbackDuration);
      $("lyricLine").textContent = (data.current_lyrics || []).slice(0, 2).join(" / ");
      if (p.coverUrl) $("nowCover").src = p.coverUrl;
      $("nowCover").style.display = p.name ? "block" : "none";
      if (playerOverlayOpen) renderPlayerPage(currentContext);
      return true;
    }
    async function syncContextFromCli({ requirePlayerOverlay = false, allowExternalChange = false } = {}) {
      if ((requirePlayerOverlay && !playerOverlayOpen) || lyricSyncInFlight) return;
      if (playbackProgressPending && !allowExternalChange) return;
      lyricSyncInFlight = true;
      try {
        await refresh({ allowExternalChange });
      } catch {
        // Keep the local UI alive if the player status is briefly unavailable.
      } finally {
        lyricSyncInFlight = false;
      }
    }
    function syncLyricFromCli() {
      return syncContextFromCli({ requirePlayerOverlay: true });
    }
    function buildWave() {
      const bars = [];
      for (let i = 0; i < 64; i++) {
        const h = 8 + Math.round(Math.abs(Math.sin(i * 1.7)) * 22);
        bars.push('<div class="bar ' + (i < 24 ? "hot" : "") + '" style="height:' + h + 'px"></div>');
      }
      $("wave").innerHTML = bars.join("");
    }
    async function searchKeyword() {
      const keyword = $("keyword").value.trim();
      if (!keyword) return;
      $("status").textContent = "搜索中";
      $("searchTitle").textContent = "搜索：" + keyword;
      $("searchMeta").textContent = "正在查找歌曲...";
      $("searchResults").innerHTML = '<div class="empty">正在搜索网易云音乐...</div>';
      showView("search");
      const data = await api("/api/search?keyword=" + encodeURIComponent(keyword) + "&limit=24");
      renderSearchResults(data);
      $("status").textContent = (data.tracks || []).length ? "搜索完成" : "没有结果";
    }
    function sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }
    async function syncPlaybackStart() {
      for (let attempt = 0; attempt < 10; attempt += 1) {
        await refresh();
        if (!playbackProgressPending) return;
        await sleep(350);
      }
    }
    async function playTrack(id, { optimisticTrack = null } = {}) {
      $("status").textContent = "播放中";
      optimisticPlayback(optimisticTrack || findKnownTrack(id), id);
      const result = await api("/api/play-track", { id });
      if (volumeUserControlled) {
        void applyPlayerVolume(playbackVolume).catch(() => {});
      }
      const durationMs = Number(result?.playback?.durationMs ?? 0);
      if (durationMs) playbackDuration = durationMs / 1000;
      const requestedId = String(id);
      const resultPlayback = result?.playback || null;
      const resultMatchesRequest = resultPlayback?.id && String(resultPlayback.id) === requestedId;
      const alreadySynced = !playbackProgressPending && currentContext?.playback?.id && String(currentContext.playback.id) === requestedId;
      const nextPosition = alreadySynced ? computedPlaybackPosition() : 0;
      currentContext = {
        ...(currentContext || {}),
        success: true,
        active: true,
        paused: false,
        position: nextPosition,
        duration: playbackDuration,
        playback: resultMatchesRequest ? resultPlayback : (currentContext?.playback || {}),
        lyrics: resultMatchesRequest ? (result?.lyrics || currentContext?.lyrics || []) : (currentContext?.lyrics || []),
        current_lyrics: [],
      };
      playbackActive = true;
      playbackPaused = false;
      playbackPosition = nextPosition;
      playbackSyncedAt = Date.now();
      autoAdvanceArmed = true;
      if (currentContext.playback?.name) $("song").textContent = currentContext.playback.name;
      $("meta").textContent = currentContext.playback?.artist || "正在播放";
      if (currentContext.playback?.coverUrl) {
        $("nowCover").src = currentContext.playback.coverUrl;
        $("nowCover").style.display = "block";
      }
      setPlaybackButtonIcon(true, false);
      if (playerOverlayOpen) renderPlayerPage(currentContext);
      renderQueue();
      void syncPlaybackStart().catch(() => {});
    }
    async function replayTrack(id, { optimisticTrack = null } = {}) {
      $("status").textContent = "重播中";
      optimisticPlayback(optimisticTrack || findKnownTrack(id), id);
      armReplayGuard(id);
      const result = await api("/api/replay-track", { id });
      const durationMs = Number(result?.playback?.durationMs ?? 0);
      if (durationMs) playbackDuration = durationMs / 1000;
      const requestedId = String(id);
      const alreadySynced = !playbackProgressPending && currentContext?.playback?.id && String(currentContext.playback.id) === requestedId;
      const nextPosition = alreadySynced ? computedPlaybackPosition() : 0;
      playbackActive = true;
      playbackPaused = false;
      playbackPosition = nextPosition;
      playbackSyncedAt = Date.now();
      armReplayGuard(id);
      autoAdvanceArmed = true;
      setPlaybackButtonIcon(true, false);
      updateProgressUi(playbackPosition, playbackDuration);
      if (playerOverlayOpen && currentContext) renderPlayerPage(currentContext);
      renderQueue();
      void syncPlaybackStart().catch(() => {});
    }
    async function playPlaylist(id) {
      const first = firstPlayableTrack();
      if (first?.id) {
        setPlaybackQueue(currentPlaylist?.name || "歌单", currentPlaylistTracks, 0);
        await playQueueIndex(0);
        return;
      }
      $("status").textContent = "播放歌单";
      await api("/api/play-playlist", { id });
      await refresh();
    }
    document.addEventListener("click", async (event) => {
      const queueItem = event.target.closest("[data-queue-index]");
      const track = event.target.closest("[data-track]");
      const playlist = event.target.closest("[data-playlist]");
      try {
        if (queueItem) {
          await playQueueIndex(Number(queueItem.dataset.queueIndex));
          return;
        }
        if (track) {
          if (track.dataset.likedIndex !== undefined) {
            const start = Number(track.dataset.likedIndex || 0);
            setPlaybackQueue("我喜欢的音乐", dashboard.liked_tracks || [], start);
            await playQueueIndex(start);
          } else if (track.dataset.playlistIndex !== undefined) {
            const start = Number(track.dataset.playlistIndex || 0);
            setPlaybackQueue(currentPlaylist?.name || "歌单", currentPlaylistTracks || [], start);
            await playQueueIndex(start);
          } else {
            queueIndex = playbackQueue.findIndex((item) => String(item.id) === String(track.dataset.track));
            renderQueue();
            await playTrack(track.dataset.track);
          }
          return;
        }
        if (playlist) await openPlaylist(playlist.dataset.playlist);
      } catch (error) {
        $("status").textContent = error.message || "播放失败";
      }
    });
    $("searchButton").onclick = () => { void searchKeyword().catch((error) => { $("status").textContent = error.message; }); };
    $("pause").onclick = async () => {
      const previousState = {
        active: playbackActive,
        paused: playbackPaused,
        position: playbackPosition,
        syncedAt: playbackSyncedAt,
        context: currentContext,
      };
      try {
        optimisticTogglePlaybackButton();
        const data = await api("/api/pause", {});
        if (data?.result?.mode === "restart" && data.result.playback) {
          playbackControlPendingUntil = 0;
          playbackControlGuard = null;
          optimisticPlayback(data.result.playback, data.result.playback.id);
          currentContext = {
            ...(currentContext || {}),
            success: true,
            active: true,
            paused: false,
            position: 0,
            duration: data.result.duration || playbackDuration,
            playback: data.result.playback,
            lyrics: data.result.lyrics || currentContext?.lyrics || [],
            current_lyrics: [],
          };
          await syncPlaybackStart();
        } else {
          void refresh().catch(() => {});
        }
      } catch (error) {
        playbackControlPendingUntil = 0;
        playbackControlGuard = null;
        playbackActive = previousState.active;
        playbackPaused = previousState.paused;
        playbackPosition = previousState.position;
        playbackSyncedAt = previousState.syncedAt;
        currentContext = previousState.context;
        setPlaybackButtonIcon();
        updateProgressUi(playbackPosition, playbackDuration);
        if (playerOverlayOpen && currentContext) renderPlayerPage(currentContext);
        $("status").textContent = error.message || "播放失败";
      }
    };
    if ($("stop")) $("stop").onclick = async () => { await api("/api/stop", {}); await refresh(); };
    $("backHome").onclick = () => showView("home");
    $("backSearchHome").onclick = () => showView("home");
    $("backFromPlay").onclick = collapsePlayerPage;
    $("playAll").onclick = () => { if (currentPlaylist?.id) void playPlaylist(currentPlaylist.id).catch((error) => { $("status").textContent = error.message; }); };
    $("nowCover").onclick = openPlayerPage;
    $("prevTrack").onclick = () => { void playPreviousFromQueue().catch((error) => { $("status").textContent = error.message || "上一首失败"; }); };
    $("nextTrack").onclick = () => { void playNextFromQueue().catch((error) => { $("status").textContent = error.message || "下一首失败"; }); };
    $("sequenceMode").onclick = () => setPlayMode("sequence");
    $("shuffleMode").onclick = () => setPlayMode("shuffle");
    $("queueToggle").onclick = () => {
      renderQueue();
      $("queuePanel").classList.toggle("hidden");
      $("queueToggle").classList.toggle("active", !$("queuePanel").classList.contains("hidden"));
    };
    $("prevRecPage").onclick = () => {
      const totalPages = Math.max(1, Math.ceil((dashboard.recommended_playlists || []).length / recPageSize));
      recPage = (recPage - 1 + totalPages) % totalPages;
      renderRecommended();
    };
    $("nextRecPage").onclick = () => {
      void nextRecommendedPage();
    };
    $("keyword").addEventListener("keydown", (event) => {
      if (event.key === "Enter") $("searchButton").click();
    });
    $("playlists").addEventListener("scroll", maybeLoadMorePlaylists);
    $("charts").addEventListener("scroll", maybeLoadMoreLikedTracks);
    $("playerTimeline").addEventListener("mouseenter", updateTimelineHover);
    $("playerTimeline").addEventListener("mousemove", updateTimelineHover);
    $("playerTimeline").addEventListener("mouseleave", () => {
      timelineHovering = false;
      $("playerTimelineTip").textContent = clockText(computedPlaybackPosition()) + " / " + (playbackDuration ? clockText(playbackDuration) : "--:--");
    });
    $("playerTimeline").addEventListener("click", async (event) => {
      if (!playbackDuration) return;
      const rect = $("playerTimeline").getBoundingClientRect();
      const ratio = rect.width ? Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) : 0;
      const seconds = Math.round(ratio * playbackDuration);
      const previousPosition = playbackPosition;
      const previousSyncedAt = playbackSyncedAt;
      playbackPosition = seconds;
      playbackSyncedAt = Date.now();
      playbackSeekGuard = {
        trackId: currentContext?.playback?.id ? String(currentContext.playback.id) : "",
        position: seconds,
        expiresAt: Date.now() + 6000,
      };
      updateProgressUi(seconds, playbackDuration);
      updateLyricHighlight(seconds);
      if (playerOverlayOpen && currentContext) {
        currentContext = { ...currentContext, position: seconds };
      }
      try {
        await api("/api/seek", { seconds });
        void refresh().catch(() => {});
      } catch (error) {
        if (playbackSeekGuard?.position === seconds) {
          playbackSeekGuard = null;
        }
        playbackPosition = previousPosition;
        playbackSyncedAt = previousSyncedAt;
        updateProgressUi(computedPlaybackPosition(), playbackDuration);
        updateLyricHighlight(computedPlaybackPosition());
        $("status").textContent = error.message || "跳转失败";
      }
    });
    $("volumeSlider").addEventListener("input", (event) => {
      volumeUserControlled = true;
      const volume = normalizeVolumeValue(event.target.value);
      if (volume === null) return;
      updateVolumeUi(volume);
      schedulePlayerVolume(volume);
    });
    $("volumeSlider").addEventListener("change", async (event) => {
      volumeUserControlled = true;
      const volume = normalizeVolumeValue(event.target.value);
      if (volume === null) return;
      updateVolumeUi(volume);
      if (volumeApplyTimer) {
        clearTimeout(volumeApplyTimer);
        volumeApplyTimer = null;
      }
      try {
        await applyPlayerVolume(volume);
      } catch (error) {
        $("status").textContent = error.message || "音量设置失败";
      }
    });
    window.addEventListener("resize", () => updateResponsiveMetrics({ rerender: true }));
    updateVolumeUi(playbackVolume);
    updatePlayModeUi();
    buildWave();
    loadDashboard().catch((error) => { $("charts").innerHTML = '<div class="empty">' + escapeHtml(error.message) + '</div>'; });
    refresh().catch(() => {});
    setInterval(() => { void syncContextFromCli({ allowExternalChange: true }); }, 1000);
    setInterval(() => { void syncLyricFromCli(); }, 3000);
    requestAnimationFrame(updateLocalProgress);
  </script>
</body>
</html>`;
}

function acceptWebClient(req) {
  const token = String(req.headers["x-web-player-client"] ?? "").trim();
  if (!token) return;
  activeWebClientToken = token;
  activeWebClientSeenAt = Date.now();
}

function validateWebPlaybackClient(req) {
  const token = String(req.headers["x-web-player-client"] ?? "").trim();
  const activeFresh = activeWebClientToken && Date.now() - activeWebClientSeenAt < 12 * 60 * 60 * 1000;
  return !activeFresh || token === activeWebClientToken;
}

async function handleWebApi(req, res) {
  const url = new URL(req.url, "http://localhost");
  try {
    if (req.method === "GET" && url.pathname === "/") {
      htmlResponse(res, playerHtml());
      return;
    }
    acceptWebClient(req);
    if (req.method === "GET" && url.pathname === "/api/context") {
      jsonResponse(res, 200, await currentListeningContext());
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/status") {
      const [status, state] = await Promise.all([getPlayerStatus(), readState()]);
      jsonResponse(res, 200, { success: true, status: status.data, playback: state ? playbackInfo(state) : null, listening: state });
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/dashboard") {
      jsonResponse(res, 200, await getDashboardData());
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/playlists") {
      const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0) || 0);
      const limit = Math.max(1, Math.min(50, Number(url.searchParams.get("limit") ?? 24) || 24));
      jsonResponse(res, 200, await getPlaylistPageData(offset, limit));
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/recommended-playlists") {
      const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0) || 0);
      const limit = Math.max(1, Math.min(50, Number(url.searchParams.get("limit") ?? 14) || 14));
      jsonResponse(res, 200, await getRecommendedPlaylistPageData(offset, limit));
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/liked-tracks") {
      const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0) || 0);
      const limit = Math.max(1, Math.min(50, Number(url.searchParams.get("limit") ?? 24) || 24));
      jsonResponse(res, 200, await getLikedTracksPageData(offset, limit));
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/playlist") {
      const id = String(url.searchParams.get("id") ?? "").trim();
      if (!id) {
        jsonResponse(res, 400, { success: false, message: "id is required" });
        return;
      }
      jsonResponse(res, 200, await getPlaylistViewData(id));
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/search") {
      const keyword = String(url.searchParams.get("keyword") ?? "").trim();
      const limit = Math.max(1, Math.min(30, Number(url.searchParams.get("limit") ?? 24) || 24));
      if (!keyword) {
        jsonResponse(res, 400, { success: false, message: "keyword is required" });
        return;
      }
      const tracks = await enrichTracks(await searchTracks(keyword, limit), limit);
      jsonResponse(res, 200, { success: true, keyword, tracks });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/play") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const body = await readRequestJson(req);
      const keyword = String(body.keyword ?? "").trim();
      if (!keyword) {
        jsonResponse(res, 400, { success: false, message: "keyword is required" });
        return;
      }
      const tracks = await searchTracks(keyword, 10);
      if (tracks.length === 0) {
        jsonResponse(res, 404, { success: false, message: `No search results for ${keyword}` });
        return;
      }
      const state = await playTrackById(tracks[0].id, { quality: body.quality ?? "exhigh", style: body.style ?? "" });
      jsonResponse(res, 200, {
        success: true,
        selected: tracks[0],
        playback: playbackInfo(state),
        lyrics: state.lyrics ?? [],
        position: 0,
        duration: Number(state.durationMs ?? 0) / 1000,
        active: true,
        paused: false,
        ai_context: buildStartContext(state),
      });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/play-track") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const body = await readRequestJson(req);
      const id = String(body.id ?? "").trim();
      if (!id) {
        jsonResponse(res, 400, { success: false, message: "id is required" });
        return;
      }
      const cachedState = await readState();
      let state;
      if (cachedState?.id && String(cachedState.id) === id) {
        try {
          state = await replayCurrentTrackFromStart(id);
        } catch {
          state = await playTrackById(id, { quality: body.quality ?? "exhigh" });
        }
      } else {
        state = await playTrackById(id, { quality: body.quality ?? "exhigh" });
      }
      jsonResponse(res, 200, {
        success: true,
        playback: playbackInfo(state),
        lyrics: state.lyrics ?? [],
        position: 0,
        duration: Number(state.durationMs ?? 0) / 1000,
        active: true,
        paused: false,
        ai_context: buildStartContext(state),
      });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/replay-track") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const body = await readRequestJson(req);
      const id = String(body.id ?? "").trim();
      if (!id) {
        jsonResponse(res, 400, { success: false, message: "id is required" });
        return;
      }
      const state = await replayCurrentTrackFromStart(id);
      jsonResponse(res, 200, {
        success: true,
        playback: playbackInfo(state),
        lyrics: state.lyrics ?? [],
        position: 0,
        duration: Number(state.durationMs ?? 0) / 1000,
        active: true,
        paused: false,
        ai_context: buildStartContext(state),
      });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/play-playlist") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const body = await readRequestJson(req);
      const id = String(body.id ?? "").trim();
      if (!id) {
        jsonResponse(res, 400, { success: false, message: "id is required" });
        return;
      }
      const detail = await getPlaylistDetail(id, 1);
      const track = detail.tracks?.[0];
      if (!track?.id) {
        jsonResponse(res, 404, { success: false, message: "Playlist has no playable tracks" });
        return;
      }
      const state = await playTrackById(track.id, { quality: body.quality ?? "exhigh" });
      jsonResponse(res, 200, { success: true, playlist: normalizePlaylistCard(detail, detail), selected: track, playback: playbackInfo(state), ai_context: buildStartContext(state) });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/pause") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const result = await toggleOrRestartPlayback();
      jsonResponse(res, 200, { success: true, result });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/seek") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const body = await readRequestJson(req);
      const seconds = Math.max(0, Math.round(Number(body.seconds ?? 0)));
      if (!Number.isFinite(seconds)) {
        jsonResponse(res, 400, { success: false, message: "Invalid seek position" });
        return;
      }
      const result = parseJson((await runNetease(["--pretty", "player", "seek", "--absolute", String(seconds)])).stdout, "neteasecli player seek");
      jsonResponse(res, 200, { success: true, position: seconds, result });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/volume") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      const body = await readRequestJson(req);
      const volume = Math.max(0, Math.min(100, Math.round(Number(body.volume ?? 80))));
      if (!Number.isFinite(volume)) {
        jsonResponse(res, 400, { success: false, message: "Invalid volume" });
        return;
      }
      const result = parseJson((await runNetease(["--pretty", "player", "volume", String(volume)])).stdout, "neteasecli player volume");
      jsonResponse(res, 200, { success: true, volume, result });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/stop") {
      if (!validateWebPlaybackClient(req)) {
        jsonResponse(res, 409, { success: false, message: "Ignored playback request from an older web player tab" });
        return;
      }
      clearPendingPlaybackState();
      clearReplayRequestGuard();
      const result = parseJson((await runNetease(["--pretty", "player", "stop"])).stdout, "neteasecli player stop");
      await clearState();
      jsonResponse(res, 200, { success: true, result });
      return;
    }
    jsonResponse(res, 404, { success: false, message: "Not found" });
  } catch (error) {
    jsonResponse(res, 500, { success: false, message: String(error.message ?? error) });
  }
}

async function startWebPlayer(port = 8765) {
  if (webPlayerServer) {
    return { port: webPlayerPort, url: `http://127.0.0.1:${webPlayerPort}/`, reused: true };
  }
  webPlayerServer = http.createServer((req, res) => {
    void handleWebApi(req, res);
  });
  await new Promise((resolve, reject) => {
    webPlayerServer.once("error", reject);
    webPlayerServer.listen(port, "127.0.0.1", () => resolve());
  });
  webPlayerPort = webPlayerServer.address().port;
  return { port: webPlayerPort, url: `http://127.0.0.1:${webPlayerPort}/`, reused: false };
}

async function ensureEnvironment() {
  const mpv = await findMpv();
  const neteaseCliCommand = await commandExists("neteasecli") || await commandExists("netease");
  return {
    node: process.version,
    rootDir,
    mpvAvailable: mpv.available,
    mpvPath: mpv.path,
    mpvSource: mpv.source,
    neteaseCliCommand,
    neteaseCliScript,
    neteaseCliInstalled: await exists(neteaseCliScript),
  };
}

async function getNeteaseAuthStatus() {
  try {
    return parseJson((await runNetease(["--pretty", "auth", "check"])).stdout, "neteasecli auth check");
  } catch (error) {
    return { success: false, error: String(error.message ?? error) };
  }
}

function psQuote(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function buildNeteaseLoginGuide(env, auth, profile = "") {
  const loginCommand = profile
    ? `neteasecli auth login --profile ${psQuote(profile)}`
    : "neteasecli auth login";
  const fallbackLoginCommand = profile
    ? `node ${psQuote(neteaseCliScript)} auth login --profile ${psQuote(profile)}`
    : `node ${psQuote(neteaseCliScript)} auth login`;

  const steps = [];
  if (!env.neteaseCliInstalled) {
    steps.push({
      status: "todo",
      title: "Install neteasecli",
      command: "npm install -g neteasecli",
      detail: "Install the NetEase CLI globally first.",
    });
  } else {
    steps.push({
      status: "done",
      title: "Install neteasecli",
      detail: `Found neteasecli at ${env.neteaseCliScript}.`,
    });
  }

  if (!env.neteaseCliCommand && env.neteaseCliInstalled) {
    steps.push({
      status: "todo",
      title: "Fix PATH or use direct node command",
      command: fallbackLoginCommand,
      detail: "The neteasecli package is installed, but the neteasecli command is not available in PATH. On Windows, add %APPDATA%\\npm to PATH, or use the direct node command below.",
    });
  }

  steps.push({
    status: "manual",
    title: "Log in to NetEase Cloud Music in your browser",
    detail: "neteasecli imports cookies from an already logged-in browser session, so open music.163.com or the NetEase web player in Chrome/Edge first and make sure you are logged in.",
  });

  steps.push({
    status: auth?.success ? "done" : "todo",
    title: "Import browser cookies with neteasecli",
    command: env.neteaseCliCommand ? loginCommand : fallbackLoginCommand,
    detail: "Run this in PowerShell. If multiple browser profiles exist, pass a Chrome/Edge profile name with --profile.",
  });

  steps.push({
    status: auth?.success ? "done" : "todo",
    title: "Verify login",
    command: env.neteaseCliCommand ? "neteasecli --pretty auth check" : `node ${psQuote(neteaseCliScript)} --pretty auth check`,
    detail: "After login, run this check or ask Claude to call setup_netease_login again.",
  });

  const ready = Boolean(env.neteaseCliInstalled && auth?.success);
  return {
    ready,
    auth,
    steps,
    ai_message: ready
      ? "neteasecli is installed and logged in. You can now search playlists, play songs, and open the web player."
      : "neteasecli is not fully ready yet. Guide the user through the todo steps, then call this tool again to verify login.",
  };
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

const server = new McpServer({
  name: "netease-music-mcp",
  version: "0.1.0",
});

const searchLimitInput = z.preprocess(
  (value) => value === undefined || value === null || value === "" ? 10 : Number(value),
  z.number().int().min(1).max(30),
);
const resultIndexInput = z.preprocess(
  (value) => value === undefined || value === null || value === "" ? 0 : Number(value),
  z.number().int().min(0).max(29),
);

server.registerTool("check_environment", {
  title: "Check local music environment",
  description: "Check neteasecli, mpv, and login status.",
  inputSchema: {},
}, async () => {
  const env = await ensureEnvironment();
  const auth = env.neteaseCliInstalled ? await getNeteaseAuthStatus() : null;
  return textResult({ success: true, environment: env, auth });
});

server.registerTool("setup_netease_login", {
  title: "Guide NetEase CLI login",
  description: "Use this when the user needs to set up, log in, or troubleshoot neteasecli. It checks installation/login state and returns step-by-step commands Claude can guide the user through.",
  inputSchema: {
    profile: z.string().optional().describe("Optional Chrome/Edge browser profile name for neteasecli auth login --profile."),
  },
}, async ({ profile }) => {
  const env = await ensureEnvironment();
  const auth = env.neteaseCliInstalled ? await getNeteaseAuthStatus() : null;
  return textResult({
    success: true,
    environment: env,
    ...buildNeteaseLoginGuide(env, auth, profile),
  });
});

server.registerTool("search_song", {
  title: "Search NetEase songs",
  description: "Search songs using neteasecli and return normalized NetEase track IDs.",
  inputSchema: {
    keyword: z.string().min(1).describe("Song, artist, or natural-language search keywords."),
    limit: searchLimitInput.describe("Result count, accepts a number or numeric string."),
  },
}, async ({ keyword, limit }) => {
  try {
    const tracks = await searchTracks(keyword, limit);
    return textResult({ success: true, tracks });
  } catch (error) {
    return failResult(String(error.message ?? error), {
      hint: "Ask Claude to call netease-music-mcp.setup_netease_login if neteasecli is not installed, not logged in, or not readable.",
    });
  }
});

server.registerTool("play_song", {
  title: "Search and play song",
  description: "Search for a song, play the best match with neteasecli/mpv, and return song info plus listening context. The style field is filled from NetEase song wiki when available.",
  inputSchema: {
    keyword: z.string().min(1).describe("Song request, for example '编号89757 林俊杰'."),
    quality: z.enum(["standard", "higher", "exhigh", "lossless", "hires"]).default("exhigh"),
    style: z.string().optional().describe("Optional fallback style/genre hint. NetEase song wiki style is used first when available."),
    resultIndex: resultIndexInput.describe("Zero-based search result index to play, accepts a number or numeric string."),
  },
}, async ({ keyword, quality, style, resultIndex }) => {
  let tracks = [];
  try {
    tracks = await searchTracks(keyword, Math.max(10, resultIndex + 1));
    if (tracks.length === 0) {
      return failResult(`No search results for ${keyword}`);
    }
  } catch (error) {
    return failResult(String(error.message ?? error), {
      hint: "Ask Claude to call netease-music-mcp.setup_netease_login if neteasecli is not installed, not logged in, or not readable.",
    });
  }
  const selected = tracks[resultIndex] ?? tracks[0];
  const state = await playTrackById(selected.id, { quality, style });
  return textResult({
    success: true,
    selected,
    playback: playbackInfo(state),
    ai_context: buildStartContext(state),
  });
});

server.registerTool("play_track", {
  title: "Play track by ID",
  description: "Play a NetEase numeric song ID with neteasecli/mpv and return song info plus the start listening context. The style field is filled from NetEase song wiki when available.",
  inputSchema: {
    id: z.union([z.string(), z.number()]).describe("NetEase numeric track ID."),
    quality: z.enum(["standard", "higher", "exhigh", "lossless", "hires"]).default("exhigh"),
    style: z.string().optional().describe("Optional fallback style/genre hint. NetEase song wiki style is used first when available."),
  },
}, async ({ id, quality, style }) => {
  const state = await playTrackById(String(id), { quality, style });
  return textResult({
    success: true,
    playback: playbackInfo(state),
    ai_context: buildStartContext(state),
  });
});

server.registerTool("next_song", {
  title: "Cut to another song",
  description: "Switch to another requested song by searching and playing it. Use this for natural-language '切歌'.",
  inputSchema: {
    keyword: z.string().min(1).describe("The next song request."),
    quality: z.enum(["standard", "higher", "exhigh", "lossless", "hires"]).default("exhigh"),
    style: z.string().optional().describe("Optional fallback style/genre hint. NetEase song wiki style is used first when available."),
  },
}, async ({ keyword, quality, style }) => {
  let tracks = [];
  try {
    tracks = await searchTracks(keyword, 10);
    if (tracks.length === 0) {
      return failResult(`No search results for ${keyword}`);
    }
  } catch (error) {
    return failResult(String(error.message ?? error), {
      hint: "Ask Claude to call netease-music-mcp.setup_netease_login if neteasecli is not installed, not logged in, or not readable.",
    });
  }
  const selected = tracks[0];
  const state = await playTrackById(selected.id, { quality, style });
  return textResult({
    success: true,
    selected,
    playback: playbackInfo(state),
    ai_context: buildStartContext(state),
  });
});

server.registerTool("pause", {
  title: "Pause playback",
  description: "Pause current neteasecli/mpv playback.",
  inputSchema: {},
}, async () => {
  const status = await getPlayerStatus();
  if (status?.data?.paused) {
    return textResult({ success: true, message: "Already paused", status: status.data });
  }
  if (!status?.data?.playing) {
    return textResult({ success: true, message: "Nothing is currently playing", status: status.data });
  }
  const result = parseJson((await runNetease(["--pretty", "player", "pause"])).stdout, "neteasecli player pause");
  return textResult({ success: true, result });
});

server.registerTool("resume", {
  title: "Resume playback",
  description: "Resume paused neteasecli/mpv playback.",
  inputSchema: {},
}, async () => {
  const status = await getPlayerStatus();
  if (status?.data?.playing && !status?.data?.paused) {
    return textResult({ success: true, message: "Already playing", status: status.data });
  }
  const result = parseJson((await runNetease(["--pretty", "player", "pause"])).stdout, "neteasecli player pause");
  return textResult({ success: true, result });
});

server.registerTool("stop", {
  title: "Stop playback",
  description: "Stop current neteasecli/mpv playback and clear listening state.",
  inputSchema: {},
}, async () => {
  const result = await stopPlaybackBestEffort();
  return textResult({ success: true, ...result });
});

server.registerTool("shutdown", {
  title: "End listening session",
  description: "Use this when the user asks to end the listening session, stop listening to music, close the player, or stop the music program. It stops mpv playback, clears listening state, and closes the current web player server, while keeping the MCP tool process alive for future calls.",
  inputSchema: {},
}, async () => {
  const [playback, webPlayer] = await Promise.all([
    stopPlaybackBestEffort(),
    closeWebPlayerBestEffort(),
  ]);
  return textResult({
    success: true,
    message: "Stopped playback and closed the current web player. The MCP server is still running for future calls.",
    playback,
    webPlayer,
  });
});

server.registerTool("get_status", {
  title: "Get playback status",
  description: "Get current neteasecli/mpv playback status plus cached song metadata.",
  inputSchema: {},
}, async () => {
  const [status, state] = await Promise.all([getPlayerStatus(), readState()]);
  return textResult({ success: true, status: status.data, playback: state ? playbackInfo(state) : null, listening: state });
});

server.registerTool("get_listening_context", {
  title: "Get current listening context",
  description: "Return the context that the AI should include on the next user message while music is playing. The lyric context is the next 6 lyric lines at or after the current playback time.",
  inputSchema: {
    before: z.number().int().min(0).max(10).default(0).describe("Deprecated. Kept for compatibility; lyric context now uses upcoming lines only."),
    after: z.number().int().min(1).max(10).default(6).describe("Number of upcoming lyric lines to include by default."),
  },
}, async ({ before, after }) => {
  return textResult(await currentListeningContext(before, after));
});

server.registerTool("open_web_player", {
  title: "Open local web player",
  description: "Generate and serve a local web player UI for search/play/pause/stop and lyric context. Returns a localhost URL.",
  inputSchema: {
    port: z.number().int().min(1024).max(65535).default(8765),
  },
}, async ({ port }) => {
  const web = await startWebPlayer(port);
  return textResult({
    success: true,
    ...web,
    message: `Web player is available at ${web.url}`,
  });
});

async function selfTest() {
  const env = await ensureEnvironment();
  console.log(JSON.stringify(env, null, 2));
}

async function main() {
  if (process.argv.includes("--self-test")) {
    await selfTest();
    return;
  }
  if (process.argv.includes("--web-player")) {
    const portIndex = process.argv.indexOf("--port");
    const port = portIndex >= 0 ? Number(process.argv[portIndex + 1]) : 8765;
    const web = await startWebPlayer(Number.isFinite(port) ? port : 8765);
    console.log(web.url);
    return;
  }
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("netease-music-mcp running on stdio");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
