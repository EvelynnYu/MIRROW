package com.mirrow.app.widget;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.view.View;
import android.widget.RemoteViews;

import com.mirrow.app.BuildConfig;
import com.mirrow.app.MainActivity;
import com.mirrow.app.R;
import com.mirrow.app.network.BackendEndpointPool;

import org.json.JSONObject;

import java.io.File;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.Locale;
import java.util.Scanner;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

public final class WanderWidget extends AppWidgetProvider {
    private static final String REFRESH = "com.mirrow.app.widget.REFRESH";
    private static final String PREF = "wander_widget_snapshot";
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static final ExecutorService io = Executors.newSingleThreadExecutor();
    private static final OkHttpClient http = new OkHttpClient.Builder()
            .followRedirects(false).callTimeout(4, TimeUnit.SECONDS).build();
    private static final ArrayList<Runnable> finished = new ArrayList<>();
    private static boolean fetching;
    private static Context relay;
    private static JSONObject catalog, shelf;
    private static String moodFamily = "", moodEndpoint = "";
    private static long moodCheckedAt;
    private static boolean observingPhone;
    private static Runnable expireReply;
    private static long replyObservedAt;

    private static JSONObject readCatalog(Context context, String name) throws Exception {
        try (InputStream stream = context.getAssets().open(name);
             Scanner scanner = new Scanner(stream, "UTF-8").useDelimiter("\\A")) {
            return new JSONObject(scanner.next());
        }
    }

    // Affect decays on its own clock. Missing mood data must not obscure a confirmed activity.
    private static String mood(String base) {
        long now = android.os.SystemClock.elapsedRealtime();
        if (base.equals(moodEndpoint) && now - moodCheckedAt < 60000) return moodFamily;
        moodEndpoint = base;
        moodCheckedAt = now;
        Request req = new Request.Builder().url(base + "/api/affect/projection?widget=true").build();
        try (Response result = http.newCall(req).execute()) {
            moodFamily = result.isSuccessful() && result.body() != null
                    ? new JSONObject(result.body().string()).optString("family", "") : "";
        } catch (Exception e) { moodFamily = ""; }
        return moodFamily;
    }

    private static int[] ids(Context context) {
        return AppWidgetManager.getInstance(context).getAppWidgetIds(new ComponentName(context, WanderWidget.class));
    }

    // Only the existing relay keeps live updates running. The launcher can also refresh independently.
    private static final Runnable tick = new Runnable() {
        @Override public void run() {
            if (relay == null || !interactive(relay) || ids(relay).length == 0) return;
            refresh(relay, null);
            main.postDelayed(this, observingPhone ? 2500 : 10000);
        }
    };

    private static boolean interactive(Context context) {
        return context.getSystemService(PowerManager.class).isInteractive();
    }

    public static void relayStarted(Context context) {
        relay = context.getApplicationContext();
        wake();
    }

    public static void relayStopped() {
        Context last = relay;
        relay = null;
        main.removeCallbacks(tick);
        replyObservedAt = 0;
        if (last != null) render(last);
    }

    public static void wake() {
        main.removeCallbacks(tick);
        main.post(tick);
    }

    @Override public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (REFRESH.equals(action) || AppWidgetManager.ACTION_APPWIDGET_UPDATE.equals(action)) {
            if (ids(context).length == 0) return;
            render(context);
            PendingResult pending = goAsync();
            refresh(context.getApplicationContext(), pending::finish);
            wake();
            return;
        }
        super.onReceive(context, intent);
    }

    @Override public void onDisabled(Context context) {
        main.removeCallbacks(tick);
        context.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit().clear().apply();
    }

    private static void refresh(Context context, Runnable completion) {
        if (completion != null) finished.add(completion);
        if (fetching) return;
        fetching = true;
        io.execute(() -> {
            boolean changed = true;
            String observedScene = "";
            try {
                String base = BackendEndpointPool.getSelected(context, BuildConfig.MIRROW_BACKEND_FALLBACK_URL);
                Request request = new Request.Builder().url(base + "/api/wander/presence").get().build();
                try (Response response = http.newCall(request).execute()) {
                    if (!response.isSuccessful() || response.body() == null) throw new java.io.IOException();
                    JSONObject data = new JSONObject(response.body().string());
                    if (!data.has("wander") || !data.has("primary_reply_running")) throw new java.io.IOException();
                    if (catalog == null) catalog = readCatalog(context, "activity-visuals.json");
                    if (shelf == null) shelf = readCatalog(context, "shelf-visuals.json");
                    WanderBadge badge = WanderBadge.from(data, catalog, shelf);
                    String scene = badge.scene;
                    observedScene = scene;
                    String family = mood(base);
                    SharedPreferences prefs = context.getSharedPreferences(PREF, Context.MODE_PRIVATE);
                    String label = badge.label;
                    long now = System.currentTimeMillis();
                    if (!prefs.getBoolean("failed", false) && label.equals(prefs.getString("label", ""))
                            && scene.equals(prefs.getString("scene", "")) && family.equals(prefs.getString("family", ""))
                            && badge.detail.equals(prefs.getString("detail", ""))
                            && prefs.getLong("seen", 0) / 60000 == now / 60000) {
                        changed = false;
                        return;
                    }
                    prefs.edit()
                            .putString("label", label).putString("detail", badge.detail)
                            .putString("scene", scene).putString("family", family)
                            .putLong("seen", System.currentTimeMillis()).putBoolean("failed", false).apply();
                }
            } catch (Exception e) {
                SharedPreferences prefs = context.getSharedPreferences(PREF, Context.MODE_PRIVATE);
                changed = !prefs.getBoolean("failed", false);
                if (changed) prefs.edit().putBoolean("failed", true).apply();
            } finally {
                final boolean redraw = changed;
                final String observed = observedScene;
                main.post(() -> {
                    fetching = false;
                    boolean wasFresh = freshReply();
                    observingPhone = observed.equals("phone_wait") || observed.equals("phone_reply");
                    if (expireReply != null) main.removeCallbacks(expireReply);
                    replyObservedAt = observed.equals("phone_reply") ? android.os.SystemClock.elapsedRealtime() : 0;
                    if (replyObservedAt > 0) {
                        expireReply = () -> render(context);
                        main.postDelayed(expireReply, 15001);
                    }
                    try { if (redraw || wasFresh != freshReply()) render(context); }
                    finally {
                        for (Runnable callback : finished) callback.run();
                        finished.clear();
                    }
                });
            }
        });
    }

    public static File avatarFile(Context context) {
        return new File(context.getNoBackupFilesDir(), "wander-widget-avatar.png");
    }

    private static boolean freshReply() {
        return replyObservedAt > 0 && android.os.SystemClock.elapsedRealtime() - replyObservedAt < 15000;
    }

    public static void render(Context context) {
        int[] widgets = ids(context);
        if (widgets.length == 0) return;
        SharedPreferences prefs = context.getSharedPreferences(PREF, Context.MODE_PRIVATE);
        long seen = prefs.getLong("seen", 0);
        boolean failed = prefs.getBoolean("failed", false);
        String format = android.text.format.DateUtils.isToday(seen) ? "HH:mm" : "MM/dd HH:mm";
        String stamp = seen == 0 ? "尚未同步" : new SimpleDateFormat(format, Locale.CHINA).format(new Date(seen)) + " 所见";
        String last = prefs.getString("label", "状态待同步");
        String label = failed ? "暂时连不上" : last;
        String detail = failed ? (seen == 0 ? "点 ↻ 重试" : "上次 · " + last) : prefs.getString("detail", "");
        String scene = failed ? "unavailable" : prefs.getString("scene", "unavailable");
        if ("phone_reply".equals(scene) && !freshReply()) {
            scene = "unavailable";
            label = "状态待同步";
            detail = "上次 · " + last;
        }
        String family = failed ? "" : prefs.getString("family", "");
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        for (int id : widgets) {
            android.os.Bundle options = manager.getAppWidgetOptions(id);
            boolean landscape = context.getResources().getConfiguration().orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE;
            int height = options.getInt(landscape ? AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT : AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT);
            int width = options.getInt(landscape ? AppWidgetManager.OPTION_APPWIDGET_MAX_WIDTH : AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH);
            manager.updateAppWidget(id, views(context, stamp, label, detail, scene, family, compactLayout(width, height)));
        }
    }

    @Override public void onAppWidgetOptionsChanged(Context context, AppWidgetManager manager, int id, android.os.Bundle options) {
        render(context);
    }

    static boolean compactLayout(int width, int height) {
        return height > 0 && (height < 95 || (height <= 110 && width > 0 && width < 260));
    }

    static RemoteViews views(Context context, String stamp, String label, String detail, String scene, String family, boolean compact) {
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.wander_widget);
        WanderArtwork.Mood mood = WanderArtwork.Mood.from(family);
        views.setImageViewResource(R.id.wander_backdrop, mood.background);
        views.setInt(R.id.wander_tint, "setBackgroundColor", mood.tint);
        views.setTextViewText(R.id.wander_watermark, mood.icon);
        WanderArtwork.show(context, views, scene);
        views.setTextViewText(R.id.wander_identity, family.isEmpty() ? "AI" : mood.icon + " " + family);
        views.setViewVisibility(R.id.wander_mood_header, compact || family.isEmpty() ? View.GONE : View.VISIBLE);
        float density = context.getResources().getDisplayMetrics().density;
        int vertical = Math.round((compact ? 1 : 6) * density);
        views.setViewPadding(R.id.wander_bubble, Math.round(10 * density), vertical, Math.round(4 * density), vertical);
        int plateInset = Math.round((compact ? 1 : 4) * density);
        views.setViewPadding(R.id.wander_activity, Math.round(6 * density), plateInset, Math.round(5 * density), plateInset);
        views.setViewVisibility(R.id.wander_activity_title, compact ? View.GONE : View.VISIBLE);
        views.setTextViewText(R.id.wander_time, compact && !detail.isEmpty() ? detail + "  ·  " + stamp : stamp);
        views.setInt(R.id.wander_label, "setMaxLines", compact ? 1 : 2);
        views.setTextViewText(R.id.wander_label, label);
        views.setTextViewText(R.id.wander_detail, detail);
        views.setViewVisibility(R.id.wander_detail, detail.isEmpty() || compact ? View.GONE : View.VISIBLE);
        File file = avatarFile(context);
        Bitmap avatar = file.isFile() ? BitmapFactory.decodeFile(file.getPath()) : null;
        views.setViewVisibility(R.id.wander_avatar_frame, avatar == null ? View.GONE : View.VISIBLE);
        views.setViewVisibility(R.id.wander_avatar, avatar == null ? View.GONE : View.VISIBLE);
        if (avatar != null) views.setImageViewBitmap(R.id.wander_avatar, avatar);
        int flags = PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT;
        Intent open = new Intent(context, MainActivity.class).setAction(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LAUNCHER).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        views.setOnClickPendingIntent(R.id.wander_widget, PendingIntent.getActivity(context, 0, open, flags));
        views.setOnClickPendingIntent(R.id.wander_bubble, PendingIntent.getActivity(context, 1,
                new Intent(open).putExtra("wander_widget_panel", "mood"), flags));
        views.setOnClickPendingIntent(R.id.wander_activity, PendingIntent.getActivity(context, 2,
                new Intent(open).putExtra("wander_widget_panel", "activity"), flags));
        Intent update = new Intent(context, WanderWidget.class).setAction(REFRESH);
        views.setOnClickPendingIntent(R.id.wander_refresh, PendingIntent.getBroadcast(context, 0, update, flags));
        return views;
    }
}
