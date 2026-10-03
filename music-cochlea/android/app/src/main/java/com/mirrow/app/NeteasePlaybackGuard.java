package com.mirrow.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;

/** Local per-track fence survives a backend disconnect; no duration-from-command timer. */
final class NeteasePlaybackGuard {
    private static final String PREFS = "mirrow_music_playback_guard";
    private static final long ACTIVE_TTL_MS = 6L * 60L * 60L * 1000L;
    private static final long RECEIPT_TTL_MS = 60L * 1000L;
    private static final long SETTLE_TTL_MS = 3000L;
    private static final HandlerThread thread = new HandlerThread("MirrowMusicFence");
    private static final Handler handler;
    private static String token = "", title = "", artist = "";
    private static String[] aliases = new String[0];
    private static boolean active = false, finished = false, requestedPause = false;
    private static boolean abortLate = false;
    private static boolean tailObserved = false;
    private static boolean conservativeTail = false;
    private static long lateDeadline = 0;
    private static long persistedUntil = 0;
    private static long settleUntil = 0;
    private static long pendingUntil = 0;
    private static Context context;
    private static boolean restored = false;
    static { thread.start(); handler = new Handler(thread.getLooper()); }

    static synchronized void restore(Context ctx) {
        if (restored) return;
        restored = true;
        context = ctx.getApplicationContext();
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        long until = prefs.getLong("until", 0L);
        if (until <= System.currentTimeMillis()) {
            prefs.edit().clear().apply();
            return;
        }
        token = prefs.getString("token", "");
        title = prefs.getString("title", "");
        artist = prefs.getString("artist", "");
        aliases = decodeAliases(prefs.getString("aliases", "[]"));
        persistedUntil = until;
        settleUntil = prefs.getLong("settle_until", 0L);
        finished = prefs.getBoolean("finished", false);
        active = !token.isEmpty() && (!finished || settleUntil > System.currentTimeMillis());
        requestedPause = prefs.getBoolean("pause_requested", false);
        pendingUntil = prefs.getLong("pending_until", 0L);
        abortLate = false;
        tailObserved = false;
        conservativeTail = active;
        settleUntil = 0L;
        if (active) {
            Log.i("MirrowMusicFence", "restored active guard");
            handler.removeCallbacks(tick);
            handler.post(tick);
        } else if (finished) {
            Log.i("MirrowMusicFence", "restored end receipt");
        }
    }

    static synchronized void arm(Context ctx, String receipt, String expectedTitle, String expectedArtist, String[] expectedAliases) {
        arm(ctx, receipt, expectedTitle, expectedArtist, expectedAliases, false);
    }

    static synchronized void armPending(Context ctx, String receipt, String expectedTitle, String expectedArtist, String[] expectedAliases) {
        arm(ctx, receipt, expectedTitle, expectedArtist, expectedAliases, true);
    }

    private static void arm(Context ctx, String receipt, String expectedTitle, String expectedArtist, String[] expectedAliases, boolean pending) {
        context = ctx.getApplicationContext();
        restored = true;
        token = receipt; title = expectedTitle; artist = expectedArtist;
        aliases = expectedAliases.clone();
        active = true; finished = false; requestedPause = false;
        abortLate = false;
        tailObserved = false;
        conservativeTail = false;
        settleUntil = 0L;
        pendingUntil = pending ? System.currentTimeMillis() + NeteaseStartFence.GRACE_MS : 0L;
        persistedUntil = System.currentTimeMillis() + ACTIVE_TTL_MS;
        if (!token.isEmpty()) persist(false, persistedUntil); else clearPersisted();
        Log.i("MirrowMusicFence", "armed");
        handler.removeCallbacks(tick);
        handler.post(tick);
    }

    static synchronized void abortLateStart(Context ctx, String expectedTitle, String expectedArtist, String[] expectedAliases) {
        arm(ctx, "", expectedTitle, expectedArtist, expectedAliases);
        abortLate = true;
        lateDeadline = SystemClock.elapsedRealtime() + 60000L;
        clearPersisted();
    }

    static synchronized void append(JSONObject data) throws Exception {
        data.put("single_stop_supported", true);
        if (finished && persistedUntil <= System.currentTimeMillis()) {
            finished = false;
            token = "";
            clearPersisted();
        }
        // NetEase may preload the next title at position zero immediately after
        // the guard has confirmed its pause.  The opaque receipt, not the newly
        // displayed metadata, identifies which MIRROW command reached the tail.
        if (finished && !token.isEmpty()) {
            data.put("end_of_track", true);
            data.put("end_token", token);
            data.put("ended_title", title);
            data.put("ended_artist", artist);
        }
    }

    private static void persist(boolean isFinished, long until) {
        if (context == null || token.isEmpty()) return;
        JSONArray encoded = new JSONArray();
        for (String alias : aliases) encoded.put(alias);
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("token", token).putString("title", title)
                .putString("artist", artist).putString("aliases", encoded.toString())
                .putBoolean("finished", isFinished)
                .putBoolean("pause_requested", requestedPause)
                .putLong("settle_until", settleUntil)
                .putLong("pending_until", pendingUntil)
                .putLong("until", until).apply();
    }

    private static void clearPersisted() {
        if (context != null) context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().clear().apply();
    }

    private static String[] decodeAliases(String raw) {
        try {
            JSONArray values = new JSONArray(raw == null ? "[]" : raw);
            String[] result = new String[Math.min(values.length(), 10)];
            for (int i = 0; i < result.length; i++) result[i] = values.optString(i, "");
            return result;
        } catch (Exception ignored) {
            return new String[0];
        }
    }

    private static final Runnable tick = new Runnable() {
        @Override public void run() {
            synchronized (NeteasePlaybackGuard.class) {
                if (!active) return;
                if (pendingUntil > 0 && NeteaseStartFence.evaluate(pendingUntil,
                        System.currentTimeMillis(), false, false) == NeteaseStartFence.Decision.EXPIRED) {
                    active = false; clearPersisted(); return;
                }
                if (abortLate && SystemClock.elapsedRealtime() > lateDeadline) {
                    active = false; clearPersisted(); return;
                }
                if (!abortLate && persistedUntil <= System.currentTimeMillis()) {
                    active = false; clearPersisted(); return;
                }
                try {
                    MediaController c = NeteaseMusicController.findController(context);
                    if (c == null) { handler.postDelayed(this, 300); return; }
                    MediaMetadata m = c.getMetadata();
                    PlaybackState s = c.getPlaybackState();
                    if (m == null || s == null) { handler.postDelayed(this, 300); return; }
                    String actual = m.getString(MediaMetadata.METADATA_KEY_TITLE);
                    String actualArtist = m.getString(MediaMetadata.METADATA_KEY_ARTIST);
                    if (actual == null || actual.trim().isEmpty()) { handler.postDelayed(this, 200); return; }
                    boolean playing = s.getState() == PlaybackState.STATE_PLAYING;
                    boolean paused = s.getState() == PlaybackState.STATE_PAUSED;
                    boolean canPause = (s.getActions() & PlaybackState.ACTION_PAUSE) != 0;
                    // NetEase can resume its preloaded next item immediately after
                    // acknowledging the tail pause.  Keep a tiny local settle fence
                    // around the signed receipt; this does not become a general
                    // controller for later manual playback.
                    if (finished && !abortLate) {
                        if (System.currentTimeMillis() >= settleUntil) {
                            active = false;
                            return;
                        }
                        if (playing && canPause) c.getTransportControls().pause();
                        handler.postDelayed(this, 100);
                        return;
                    }
                    boolean identityMatches = NeteaseSongIdentity.matches(title, aliases, actual);
                    boolean artistMatches = actualArtist == null || artist.isEmpty() || actualArtist.isEmpty()
                            || artist.toLowerCase().contains(actualArtist.toLowerCase())
                            || actualArtist.toLowerCase().contains(artist.toLowerCase());
                    if (pendingUntil > 0) {
                        NeteaseStartFence.Decision decision = NeteaseStartFence.evaluate(pendingUntil,
                                System.currentTimeMillis(), identityMatches && artistMatches, playing);
                        if (decision == NeteaseStartFence.Decision.EXPIRED) {
                            active = false; clearPersisted(); return;
                        }
                        if (decision == NeteaseStartFence.Decision.WAIT) {
                            handler.postDelayed(this, 200); return;
                        }
                        pendingUntil = 0L;
                        persist(false, persistedUntil);
                    }
                    if (requestedPause && !abortLate && !identityMatches) {
                        if (playing && canPause) c.getTransportControls().pause();
                        if (paused) markFinished();
                        if (active) handler.postDelayed(this, 100);
                        return;
                    }
                    if (!identityMatches) {
                        if (abortLate) { handler.postDelayed(this, 200); return; }
                        Log.i("MirrowMusicFence", "released: title changed");
                        active = false; clearPersisted(); return;
                    }
                    if (!artistMatches) {
                        if (abortLate) { handler.postDelayed(this, 200); return; }
                        Log.i("MirrowMusicFence", "released: artist changed");
                        active = false; clearPersisted(); return;
                    }
                    long duration = m.getLong(MediaMetadata.METADATA_KEY_DURATION);
                    long position = s.getPosition();
                    if (playing && s.getLastPositionUpdateTime() > 0)
                        position += (long) ((SystemClock.elapsedRealtime() - s.getLastPositionUpdateTime()) * s.getPlaybackSpeed());
                    if (requestedPause && paused) {
                        Log.i("MirrowMusicFence", "pause confirmed; natural_end=" + !abortLate);
                        if (!abortLate) {
                            markFinished();
                            handler.postDelayed(this, 100);
                        } else {
                            finished = false; active = false; clearPersisted();
                        }
                        return;
                    }
                    if (!tailObserved && playing && duration > 0 && position >= duration - 3000) {
                        tailObserved = true;
                        Log.i("MirrowMusicFence", "tail observed: duration_ms=" + duration + " position_ms=" + position);
                    }
                    long stopLeadMs = conservativeTail ? 1000L : 180L;
                    if (playing && (abortLate || (duration > 0 && position >= duration - stopLeadMs))
                            && (s.getActions() & PlaybackState.ACTION_PAUSE) != 0) {
                        if (!requestedPause) Log.i("MirrowMusicFence", "pause requested: duration_ms=" + duration + " position_ms=" + position);
                        c.getTransportControls().pause();
                        requestedPause = true;
                        if (!abortLate) persist(false, persistedUntil);
                    }
                } catch (Exception ignored) { /* No fabricated finish receipt. */ }
                if (active) handler.postDelayed(this, 100);
            }
        }
    };

    private static void markFinished() {
        finished = true;
        active = true;
        long now = System.currentTimeMillis();
        settleUntil = now + SETTLE_TTL_MS;
        persistedUntil = now + RECEIPT_TTL_MS;
        persist(true, persistedUntil);
    }
}
