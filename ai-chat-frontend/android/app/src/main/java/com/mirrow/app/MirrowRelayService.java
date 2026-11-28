package com.mirrow.app;

import android.app.*;
import android.content.*;
import android.hardware.*;
import android.location.*;
import android.net.*;
import android.os.*;
import android.app.usage.*;
import android.bluetooth.*;
import android.content.pm.ServiceInfo;
import android.content.pm.PackageManager;
import android.provider.Settings;
import android.util.Log;

import org.json.JSONObject;

import com.mirrow.app.plugins.BLEController;
import com.mirrow.app.network.BackendEndpointPool;
import com.mirrow.app.relay.RelayObservabilityStore;
import com.mirrow.app.relay.RelayWatchdogController;
import com.mirrow.app.relay.RelayWatchdogStore;

import java.text.SimpleDateFormat;
import java.util.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.*;
import okio.ByteString;

/**
 * MIRROW 手机端原生后台代理 — 前台 Service 维持 WebSocket 长连接，
 * 处理后端 relay 请求（current_app / steps / gps / usage），
 * 替代 Capacitor WebView JS WebSocket（切后台不断连）。
 *
 * 抄 AionsHome AionPushService 架构，适配 MIRROW relay 协议。
 */
public class MirrowRelayService extends Service {

    // ── 常量 ──────────────────────────────────────────
    private static final String TAG = "MirrowRelay";
    private static final String CH_KEEPALIVE = "mirrow_keepalive";
    private static final String CH_MESSAGE  = "mirrow_message";

    private static final long HEARTBEAT_MS   = 45_000;   // 45s ping
    private static final long HEALTH_TIMEOUT = 120_000;  // 120s 无消息 → 重连
    private static final long ACTIVITY_INTERVAL = 60_000; // 60s 前台App检测
    private static final long GPS_INTERVAL = 600_000;     // 10min GPS
    private static final long USAGE_INTERVAL = 600_000;   // 10min 屏幕使用

    private static final int BASE_RECONNECT_DELAY = 3_000;
    private static final int MAX_RECONNECT_DELAY = 30_000; // 指数退避上限
    private static final int NOTIF_FOREGROUND = 1;
    /** Sent only by a visible MainActivity; never synthesized by a background restart. */
    public static final String ACTION_ARM_BACKGROUND_CAMERA =
            "com.mirrow.app.action.ARM_BACKGROUND_CAMERA";
    public static final String ACTION_DISARM_BACKGROUND_CAMERA =
            "com.mirrow.app.action.DISARM_BACKGROUND_CAMERA";
    /** Sent only by a visible MainActivity after RECORD_AUDIO is granted. */
    public static final String ACTION_ARM_AMBIENT_MICROPHONE =
            "com.mirrow.app.action.ARM_AMBIENT_MICROPHONE";
    public static final String PREF_BACKGROUND_CAMERA_ENABLED = "background_camera_enabled";
    private static final String PREF_RELAY_DIAGNOSTICS = "relay_diagnostics";
    private static final int MAX_RELAY_DIAGNOSTICS = 24;

    // ── 无障碍周期自愈（2026-08-22 移植 AionsHome AccessibilityRecoveryController 思路） ──
    /** 每 5min 轻检查一次无障碍；运行中 ColorOS 自动关掉时自动恢复（此前只在 onCreate 自愈一次） */
    private static final long SELF_HEAL_INTERVAL_MS = 300_000;
    /** 敏感前台包：ColorOS 在这些 App 界面会自动关无障碍，写回会再次被关 → 自愈须避让（AionsHome 同款） */
    private static final java.util.Set<String> SENSITIVE_SELF_HEAL_PACKAGES = new java.util.HashSet<>(
        java.util.Arrays.asList(
            "com.tencent.mm",               // 微信
            "com.eg.android.AlipayGphone",  // 支付宝
            "com.android.settings"          // 系统设置
        ));

    // ── 运行状态 ──────────────────────────────────────
    private boolean shouldRun = false;
    private final AtomicBoolean wsConnected = new AtomicBoolean(false);
    private final AtomicBoolean wsConnecting = new AtomicBoolean(false);
    private final AtomicInteger wsGeneration = new AtomicInteger(0);
    private volatile long lastMessageTime = 0;
    private volatile int reconnectDelay = BASE_RECONNECT_DELAY;
    private volatile long lastDisconnectTime = 0;
    private String serviceInstanceId = "";
    /** Data requests are tracked by request id so an overnight gap can distinguish
     * no request, no result, and result produced but not sent. */
    private final ConcurrentHashMap<String, String> pendingDataTools = new ConcurrentHashMap<>();

    // 重连由主线程 Handler 独立调度，不再依赖 45s 心跳线程轮询。
    private final Object reconnectLock = new Object();
    private Runnable reconnectRunnable;
    private long reconnectDueAt = 0;

    // ── 连接 ──────────────────────────────────────────
    private OkHttpClient client;
    private volatile WebSocket webSocket;
    private String serverUrl;          // 当前选中的固定后端端点
    private String wsUrl;

    // ── 电源 ──────────────────────────────────────────
    private PowerManager.WakeLock wakeLock;
    private android.net.wifi.WifiManager.WifiLock wifiLock;

    // ── 线程 ──────────────────────────────────────────
    private Thread heartbeatThread;
    private Thread activityThread;
    private Thread gpsThread;
    private Thread usageThread;
    private Thread selfHealThread;
    private Handler mainHandler;
    private final Runnable notificationChangedSender = () -> {
        WebSocket ws = webSocket;
        if (ws == null || !wsConnected.get()) return;
        try {
            JSONObject event = new JSONObject();
            event.put("type", "notification_state_change");
            // Invalidation only: no titles, contacts or message text on the event stream.
            ws.send(event.toString());
        } catch (Exception ignored) {}
    };
    // Camera state is intentionally process-local. A boot/service restart must be armed again
    // by the visible activity, otherwise Android's while-in-use permission is not trustworthy.
    private BackgroundCameraController backgroundCamera;
    private BackgroundCameraState backgroundCameraState;
    private volatile boolean cameraCapabilityAdvertised = false;
    private AmbientAudioController ambientAudio;

    // ── 传感器 ────────────────────────────────────────
    private SensorManager sensorManager;
    private Sensor stepSensor;
    private float latestStepCounter = -1;
    private float stepDayStart = -1;
    private float stepRebootOffset = 0;
    private float stepLastKnown = -1;
    private String stepLogicalDate = "";

    // ── 设备姿态/运动/光线（2026-08-24 AionsHome 移植：亮屏注册/熄屏注销）──
    // MIRROW 是 pull 模型（后端每 tick 拉取 snapshot()），无需手机端主动 push/heartbeat。
    // 单例：本服务后台采集喂数据，MobileToolPlugin 前台读取共享缓存。
    private final DeviceContextClassifier sensorClassifier = DeviceContextClassifier.getInstance();
    private Sensor gravitySensor;
    private Sensor linearAccelSensor;
    private Sensor lightSensor;
    private Sensor proximitySensor;
    private boolean deviceSensorsRegistered = false;
    private static final int SENSOR_SAMPLE_US = 150_000;  // 150ms 采样
    private static final int SENSOR_BATCH_US = 1_000_000; // 1s 批量上报

    // ── 广播 ──────────────────────────────────────────
    private ScreenReceiver screenReceiver;
    private ConnectivityManager.NetworkCallback networkCallback;
    private boolean screenOn = true;

    // ── WiFi 监控 ─────────────────────────────────────
    private android.net.wifi.WifiManager wifiManager;
    private String currentWifiSsid = null;
    // ⚡ 截图去重缓存——防止无障碍 takeScreenshot 返回 stale framebuffer
    private String _lastScreenshotHashBg = "";
    private String currentWifiBssid = null;
    private boolean currentWifiConnected = false;
    private long lastWifiEventTime = 0;
    private Runnable pendingWifiDebounce = null;
    private static final long WIFI_DEBOUNCE_MS = 3000;  // 3s 防抖
    private static final long WIFI_PENDING_MAX_AGE_MS = 30 * 60_000L;
    private static final String PREF_WIFI_PENDING = "wifi_pending_transition";

    // ── 当前应用 ──────────────────────────────────────
    private String currentAppPackage = "";
    private String currentAppName = "";
    private String currentAppResolution = "unknown";
    private long currentAppObservedAtMs = 0L;
    private String lastInteractiveAppPackage = "";
    private long lastInteractiveAppObservedAtMs = 0L;

    // ── GPS ───────────────────────────────────────────
    private LocationManager locationManager;
    private LocationListener locationListener;
    private volatile double lastLat, lastLng;
    private volatile long lastGpsTime;

    // ── 手机持续活动追踪（累积15min→push phone_activity_report 唤醒 sleeping）──
    private long activityAccumulatedSeconds = 0;       // 累积连续活动秒数
    private long activityLastCheckMs = 0;               // 上次采样时间
    private boolean activityFired = false;              // 本轮已上报（防止重复push）
    private static final long ACTIVITY_FIRE_SECONDS = 900; // 15min = 900s

    // ── SharedPreferences ─────────────────────────────
    private SharedPreferences prefs;

    // ── 音乐耳蜗：通知栏轮询 + HTTP POST ──
    private Thread musicPollThread;
    private String lastPostedSongKey = "";
    private static final long MUSIC_POLL_MS = 5000;  // 5s 轮询

    // ================================================================
    // 生命周期
    // ================================================================

    @Override
    public void onCreate() {
        super.onCreate();
        com.mirrow.app.widget.WanderWidget.relayStarted(this);
        Log.i(TAG, "MirrowRelayService onCreate");
        mainHandler = new Handler(Looper.getMainLooper());
        MirrowNotificationListener.setChangeCallback(() -> {
            mainHandler.removeCallbacks(notificationChangedSender);
            mainHandler.postDelayed(notificationChangedSender, 500);
        });
        prefs = getSharedPreferences(BackendEndpointPool.PREFS_NAME, MODE_PRIVATE);
        NeteasePlaybackGuard.restore(this);
        backgroundCamera = new BackgroundCameraController(this);
        backgroundCameraState = new BackgroundCameraState(
                prefs.getBoolean(PREF_BACKGROUND_CAMERA_ENABLED, false));
        ambientAudio = new AmbientAudioController(
                this,
                prefs,
                message -> {
                    WebSocket ws = webSocket;
                    return ws != null && wsConnected.get() && ws.send(message.toString());
                },
                microphone -> startForegroundCompat(isBackgroundCameraReady(), microphone),
                this::ambientAudioContext
        );
        serverUrl = BackendEndpointPool.getSelected(this, BuildConfig.MIRROW_BACKEND_FALLBACK_URL);
        screenOn = readScreenInteractive();
        serviceInstanceId = UUID.randomUUID().toString();
        RelayWatchdogController.onServiceStarted(this, serviceInstanceId);
        recordRelayDiagnostic("service_started", "screen_on=" + screenOn);

        initNotificationChannels();
        // 查岗闹钟是 AlarmManager 本地状态；服务启动时顺便恢复跨重启的宽限计时。
        CheckupAlarmController.restoreScheduledAlarms(this);
        initWakeLocks();
        initOkHttp();
        if (com.mirrow.app.miband.MiBandBridge.permitted(this)) {
            com.mirrow.app.miband.MiBandBridge.attach(this).resumeTransport();
        }
        initScreenReceiver();
        seedCurrentWifiState();
        initNetworkCallback();
        initSensors();
        initLocation();

        shouldRun = true;
        startForegroundCompat(false, false);
        startHeartbeatThread();
        startActivityThread();
        startGpsThread();
        startUsageThread();
        startSelfHealThread();   // ⚡ 无障碍周期自愈（5min 轻检查）

        // 音乐耳蜗：启动通知栏轮询线程
        // Music v2 polls authenticated native MediaSession receipts through Relay.
        // Do not send the retired notification-title HTTP observation stream.

        // 无障碍自愈：延迟 8s（系统绑定是异步的，立即查 isRunning() 必为 false 会误触发 toggle cycle）
        mainHandler.postDelayed(() -> {
            try {
                String r = MirrowAccessibilityService.ensureEnabled(MirrowRelayService.this);
                MainApplication.log("A11Y", "relay ensureEnabled: " + r, null);
            } catch (Throwable e) {
                MainApplication.log("A11Y", "relay ensureEnabled crash: " + e.getMessage(), e);
            }
        }, 8000);

        // 电池优化白名单引导（延迟 15s，等连接稳定后检查）
        mainHandler.postDelayed(() -> {
            try {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                    Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(intent);
                    MainApplication.log("RELAY", "battery optimization dialog shown", null);
                }
            } catch (Exception e) {
                Log.w(TAG, "battery optimization check failed: " + e.getMessage());
            }
        }, 15000);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? "" : intent.getAction();
        if (ACTION_ARM_BACKGROUND_CAMERA.equals(action)) {
            armBackgroundCameraFromVisibleActivity();
        } else if (ACTION_DISARM_BACKGROUND_CAMERA.equals(action)) {
            disarmBackgroundCamera("explicit_disarm");
        } else if (ACTION_ARM_AMBIENT_MICROPHONE.equals(action)) {
            if (ambientAudio != null) ambientAudio.armFromVisibleActivity();
        }
        if (intent != null && CheckupAlarmController.ACTION_FLUSH.equals(intent.getAction())) {
            mainHandler.post(this::flushCheckupEvents);
        }
        if (RelayWatchdogController.ACTION_REPAIR_RELAY.equals(action)) {
            String reason = intent.getStringExtra(RelayWatchdogController.EXTRA_REASON);
            mainHandler.post(() -> repairRelay(reason == null ? "unknown" : reason));
        }
        // 如果被杀死后重启，读取最新 URL
        String savedUrl = BackendEndpointPool.getSelected(this, serverUrl);
        if (savedUrl != null && !savedUrl.equals(serverUrl)) {
            serverUrl = savedUrl;
            wsUrl = null; // 强制重建
            reconnectDelay = BASE_RECONNECT_DELAY;
            disconnectWebSocket();
            scheduleReconnect("server_url_changed", 0);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        com.mirrow.app.widget.WanderWidget.relayStopped();
        MirrowNotificationListener.setChangeCallback(null);
        mainHandler.removeCallbacks(notificationChangedSender);
        // Keep exactly one system-owned recovery alarm; do not create competing 3s/5s restarts.
        if (shouldRun) {
            RelayWatchdogController.ensureLease(this, "service_destroyed");
        }

        shouldRun = false;
        BLEController.disconnectIfInitialized();
        com.mirrow.app.miband.MiBandRuntime.get(this).suspendTransport();
        cancelReconnect();
        stopContinuousMotion();  // 注销设备姿态传感器（2026-08-24）
        stopGpsThread();
        stopActivityThread();
        stopUsageThread();
        stopHeartbeatThread();
        if (backgroundCamera != null) backgroundCamera.close();
        if (ambientAudio != null) ambientAudio.close();
        if (selfHealThread != null) selfHealThread.interrupt();
        disconnectWebSocket();
        releaseWakeLocks();
        unregisterScreenReceiver();
        unregisterNetworkCallback();
        if (client != null) {
            client.dispatcher().executorService().shutdown();
            client.connectionPool().evictAll();
        }
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // Swiping the task must not introduce a second restart alarm.
        RelayWatchdogController.ensureLease(this, "task_removed");
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ================================================================
    // 通知
    // ================================================================

    private void initNotificationChannels() {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);

        NotificationChannel chKeep = new NotificationChannel(
            CH_KEEPALIVE, "MIRROW 后台保活", NotificationManager.IMPORTANCE_LOW);
        chKeep.setDescription("后台连接状态");
        nm.createNotificationChannel(chKeep);

        NotificationChannel chMsg = new NotificationChannel(
            CH_MESSAGE, "MIRROW 消息", NotificationManager.IMPORTANCE_DEFAULT);
        nm.createNotificationChannel(chMsg);
    }

    private boolean startForegroundCompat(boolean requestCameraType, boolean requestMicrophoneType) {
        String initialState = wsConnected.get() ? "WS 已连接" : "WS 连接中";
        Notification notif = new Notification.Builder(this, CH_KEEPALIVE)
            .setContentTitle("MIRROW")
            .setContentText(initialState)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setOngoing(true)
            .setPriority(Notification.PRIORITY_LOW)
            .build();

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                // specialUse avoids Android 15 dataSync quotas. Camera is added only after
                // MainActivity has explicitly armed us during its visible while-in-use window.
                int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE;
                if (requestCameraType && hasCameraPermission() && backgroundCamera != null
                        && backgroundCamera.isAvailable()) {
                    type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
                }
                if (requestMicrophoneType && hasMicrophonePermission()) {
                    type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
                }
                startForeground(NOTIF_FOREGROUND, notif, type);
            } else {
                startForeground(NOTIF_FOREGROUND, notif);
            }
            return true;
        } catch (SecurityException | IllegalArgumentException e) {
            Log.w(TAG, "foreground upgrade refused", e);
            return false;
        }
    }

    private boolean hasCameraPermission() {
        return checkSelfPermission(android.Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasMicrophonePermission() {
        return checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
    }

    /** Only called for ACTION_ARM_BACKGROUND_CAMERA emitted by the visible Activity. */
    private void armBackgroundCameraFromVisibleActivity() {
        if (backgroundCamera == null || backgroundCameraState == null || !hasCameraPermission()) {
            disarmBackgroundCamera("camera_permission_or_controller_unavailable");
            return;
        }
        // Permission grant in the foreground is the explicit device-owner opt-in. Persisting
        // that selection does not persist the arm itself; each process must be armed again.
        prefs.edit().putBoolean(PREF_BACKGROUND_CAMERA_ENABLED, true).apply();
        backgroundCameraState = new BackgroundCameraState(true);
        boolean armed = backgroundCameraState.armFromVisibleActivity(
                true, backgroundCamera.isAvailable(),
                startForegroundCompat(true, ambientAudio != null && ambientAudio.isArmed()));
        if (!armed) {
            disarmBackgroundCamera("foreground_camera_upgrade_failed");
            return;
        }
        // startForegroundCompat(true) runs before the state machine flips to armed, so refresh
        // the low-priority foreground notification once the capability is genuinely ready.
        updateKeepAlive(wsConnected.get() ? "WS 已连接" : "WS 连接中");
        Log.i(TAG, "background camera armed from visible activity");
        publishRelayCapabilities();
    }

    private boolean isBackgroundCameraReady() {
        return backgroundCameraState != null && backgroundCamera != null
                && backgroundCameraState.isReady(hasCameraPermission(), backgroundCamera.isAvailable());
    }

    private void disarmBackgroundCamera(String reason) {
        if (backgroundCameraState != null) backgroundCameraState.disarm();
        if (backgroundCamera != null) backgroundCamera.close();
        cameraCapabilityAdvertised = false;
        // Downgrade back to specialUse; do not leave a camera-type FGS advertised after revoke.
        startForegroundCompat(false, ambientAudio != null && ambientAudio.isArmed());
        Log.i(TAG, "background camera disarmed: " + reason);
        publishRelayCapabilities();
    }

    private void publishRelayCapabilities() {
        WebSocket ws = webSocket;
        if (ws != null && wsConnected.get()) sendRelayHello(ws);
    }

    private void refreshBackgroundCameraValidity() {
        if (cameraCapabilityAdvertised && !isBackgroundCameraReady()) {
            disarmBackgroundCamera("capability_no_longer_valid");
        }
    }

    /**
     * Android 15+ FGS 配额超时回调。specialUse 不该触发——防御性兜底：
     * 优雅退出好过系统抛 ForegroundServiceDidNotStopInTimeException 杀整个进程连坐无障碍服务。
     */
    @Override
    public void onTimeout(int startId, int fgsType) {
        MainApplication.log("RELAY", "FGS onTimeout fired (type=" + fgsType + ") — graceful stop", null);
        try { stopForeground(STOP_FOREGROUND_REMOVE); } catch (Throwable ignore) {}
        stopSelf();
    }

    private void updateKeepAlive(String text) {
        Notification notif = new Notification.Builder(this, CH_KEEPALIVE)
            .setContentTitle("MIRROW")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setOngoing(true)
            .setPriority(Notification.PRIORITY_LOW)
            .build();
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        nm.notify(NOTIF_FOREGROUND, notif);
    }

    // ================================================================
    // WakeLock + WifiLock
    // ================================================================

    private void initWakeLocks() {
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, TAG + ":WakeLock");
        wakeLock.acquire(); // 无限持有

        android.net.wifi.WifiManager wm = (android.net.wifi.WifiManager)
            getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        wifiLock = wm.createWifiLock(android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY, TAG + ":Wifi");
        wifiLock.acquire();
    }

    private void releaseWakeLocks() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
            if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        } catch (Exception ignored) {}
    }

    // ================================================================
    // OkHttp + WebSocket
    // ================================================================

    private void initOkHttp() {
        client = new OkHttpClient.Builder()
            .pingInterval(30, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)
            .connectTimeout(10, TimeUnit.SECONDS)
            .build();
    }

    /**
     * 记录有限长度的 relay 诊断环，便于定位 ColorOS/网络/后端造成的断线。
     * 详细异常仍进 logcat 和现有持久化日志；环形副本只保留最近 24 条。
     */
    private String sanitizeDiagnosticText(String text) {
        if (text == null) return "";
        return text
            .replaceAll("(?<!\\d)(?:\\d{1,3}\\.){3}\\d{1,3}(?!\\d)", "<ip>")
            .replaceAll("(?i)(port\\s+)\\d+", "$1*");
    }

    private boolean readScreenInteractive() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            return pm == null || pm.isInteractive();
        } catch (Exception ignored) {
            return screenOn;
        }
    }

    /** State-only context attached to local diagnostics; no sensor payload is recorded. */
    private JSONObject relayObservationState() {
        JSONObject state = new JSONObject();
        try {
            long now = System.currentTimeMillis();
            long lastResult = RelayWatchdogStore.lastDataResultAt(this);
            boolean interactive = readScreenInteractive();
            state.put("screen_on", interactive);
            state.put("screen_flag", screenOn);
            state.put("ws_connected", wsConnected.get());
            state.put("ws_connecting", wsConnecting.get());
            // A package observed before screen-off is not a foreground app
            // while the device is locked. The boundary event keeps the last
            // active package in its detail field.
            state.put("foreground_pkg", interactive && currentAppPackage != null ? currentAppPackage : "");
            state.put("last_server_ack_at", RelayWatchdogStore.lastServerAckAt(this));
            state.put("last_data_request_at", RelayWatchdogStore.lastDataRequestAt(this));
            state.put("last_data_result_at", lastResult);
            state.put("data_gap_ms", lastResult > 0 ? Math.max(0L, now - lastResult) : -1L);
        } catch (Exception ignored) {}
        return state;
    }

    /** Bounded provenance for ambient audio; never includes titles, contacts, or notification text. */
    private JSONObject ambientAudioContext() {
        JSONObject data = new JSONObject();
        try {
            long now = System.currentTimeMillis();
            boolean interactive = readScreenInteractive();
            android.media.AudioManager audio = (android.media.AudioManager)
                    getSystemService(Context.AUDIO_SERVICE);
            int mode = audio == null ? android.media.AudioManager.MODE_INVALID : audio.getMode();
            boolean mediaActive = audio != null && audio.isMusicActive();
            String kind = mode == android.media.AudioManager.MODE_IN_CALL ? "cellular_call"
                    : mode == android.media.AudioManager.MODE_IN_COMMUNICATION ? "voip_or_video_call"
                    : mode == android.media.AudioManager.MODE_RINGTONE ? "ringing"
                    : mediaActive ? "media_playback" : "ambient";

            String mediaPackage = mediaActive ? activeMediaSessionPackage() : "";
            String callPackage = activeCallNotificationPackage();
            String inputMethodPackage = "";
            String overlayPackage = MirrowAccessibilityService.getForegroundOverlayPackage();
            long overlayObservedAt = MirrowAccessibilityService.getLastWindowObservedAtMs();
            if (interactive && overlayPackage != null && !overlayPackage.isEmpty()
                    && now - overlayObservedAt <= 2 * 60_000L
                    && MirrowAccessibilityService.isInputMethodPackage(this, overlayPackage)) {
                inputMethodPackage = overlayPackage;
            }
            String sourcePackage = "";
            String sourceBasis = "unknown";
            String confidence = "unknown";
            boolean callMode = "cellular_call".equals(kind)
                    || "voip_or_video_call".equals(kind) || "ringing".equals(kind);
            if (callMode && !callPackage.isEmpty()) {
                sourcePackage = callPackage;
                sourceBasis = "active_call_notification";
                confidence = "strong";
            }
            if (mediaActive && !callMode && !mediaPackage.isEmpty()) {
                sourcePackage = mediaPackage;
                    sourceBasis = "active_media_session";
                    confidence = "confirmed";
            }
            if (!callMode && !callPackage.isEmpty()) {
                sourcePackage = callPackage;
                sourceBasis = "active_call_notification";
                confidence = "strong";
                kind = "possible_call";
            }
            if (sourcePackage.isEmpty() && interactive && currentAppPackage != null
                    && !currentAppPackage.isEmpty() && !getPackageName().equals(currentAppPackage)) {
                sourcePackage = currentAppPackage;
                sourceBasis = "foreground_app_candidate";
                confidence = "inferred";
            }
            if (sourcePackage.isEmpty() && !lastInteractiveAppPackage.isEmpty()
                    && now - lastInteractiveAppObservedAtMs <= 10 * 60_000L) {
                sourcePackage = lastInteractiveAppPackage;
                sourceBasis = "recent_app_before_lock_candidate";
                confidence = "inferred";
            }

            data.put("kind", kind);
            data.put("media_playback_active", mediaActive);
            data.put("audio_mode", audioModeName(mode));
            data.put("source_app_package", sourcePackage);
            data.put("source_app_name", sourcePackage.isEmpty() ? "" : resolveAppName(sourcePackage));
            data.put("source_basis", sourceBasis);
            data.put("source_confidence", confidence);
            data.put("media_source_app_package", mediaPackage);
            data.put("media_source_app_name", mediaPackage.isEmpty() ? "" : resolveAppName(mediaPackage));
            data.put("media_source_basis", mediaPackage.isEmpty() ? "unknown" : "active_media_session");
            data.put("call_source_app_package", callPackage);
            data.put("call_source_app_name", callPackage.isEmpty() ? "" : resolveAppName(callPackage));
            data.put("call_source_basis", callPackage.isEmpty() ? "unknown" : "active_call_notification");
            data.put("input_method_visible", !inputMethodPackage.isEmpty());
            data.put("input_method_app_package", inputMethodPackage);
            data.put("input_method_app_name", inputMethodPackage.isEmpty() ? "" : resolveAppName(inputMethodPackage));
            data.put("input_method_basis", inputMethodPackage.isEmpty() ? "unknown" : "accessibility_overlay");
            data.put("screen_on", interactive);
            data.put("observed_at_ms", now);
            data.put("aec_enabled", ambientAudio != null && ambientAudio.isPlaybackSuppressionEnabled());
        } catch (Exception ignored) {}
        return data;
    }

    private String activeMediaSessionPackage() {
        try {
            android.media.session.MediaSessionManager manager =
                    (android.media.session.MediaSessionManager) getSystemService(Context.MEDIA_SESSION_SERVICE);
            if (manager == null) return "";
            android.content.ComponentName listener =
                    new android.content.ComponentName(this, MirrowNotificationListener.class);
            for (android.media.session.MediaController controller : manager.getActiveSessions(listener)) {
                android.media.session.PlaybackState state = controller.getPlaybackState();
                int value = state == null ? android.media.session.PlaybackState.STATE_NONE : state.getState();
                if (value == android.media.session.PlaybackState.STATE_PLAYING
                        || value == android.media.session.PlaybackState.STATE_BUFFERING
                        || value == android.media.session.PlaybackState.STATE_CONNECTING) {
                    return controller.getPackageName() == null ? "" : controller.getPackageName();
                }
            }
        } catch (Exception ignored) {}
        return "";
    }

    private String activeCallNotificationPackage() {
        try {
            String[] packages = MirrowNotificationListener.getActiveCallPackages();
            if (packages.length > 0) return packages[packages.length - 1];
        } catch (Exception ignored) {}
        return "";
    }

    private static String audioModeName(int mode) {
        if (mode == android.media.AudioManager.MODE_IN_CALL) return "in_call";
        if (mode == android.media.AudioManager.MODE_IN_COMMUNICATION) return "in_communication";
        if (mode == android.media.AudioManager.MODE_RINGTONE) return "ringtone";
        if (mode == android.media.AudioManager.MODE_NORMAL) return "normal";
        return "unknown";
    }

    private void recordRelayObservation(String event, JSONObject fields) {
        try {
            RelayObservabilityStore.record(this, event, fields);
        } catch (Exception ignored) {}
    }

    private String shortRequestId(String requestId) {
        if (requestId == null) return "";
        return requestId.length() <= 24 ? requestId : requestId.substring(0, 24);
    }

    private void recordRelayDiagnostic(String event, String detail) {
        String safeDetail = detail == null ? "" : detail.replace('\n', ' ').replace('\r', ' ');
        safeDetail = sanitizeDiagnosticText(safeDetail);
        if (safeDetail.length() > 180) safeDetail = safeDetail.substring(0, 180);
        String line = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date())
            + " " + event + (safeDetail.isEmpty() ? "" : " " + safeDetail);
        Log.i(TAG, "[WS] " + line);
        MainApplication.log("WS", line, null);

        try {
            JSONObject fields = relayObservationState();
            fields.put("detail", safeDetail);
            recordRelayObservation(event, fields);
        } catch (Exception ignored) {}

        if (prefs == null) return;
        synchronized (reconnectLock) {
            try {
                String old = prefs.getString(PREF_RELAY_DIAGNOSTICS, "");
                String[] previous = old.isEmpty() ? new String[0] : old.split("\\n");
                StringBuilder next = new StringBuilder();
                int first = Math.max(0, previous.length - (MAX_RELAY_DIAGNOSTICS - 1));
                for (int i = first; i < previous.length; i++) {
                    if (next.length() > 0) next.append('\n');
                    next.append(sanitizeDiagnosticText(previous[i]));
                }
                if (next.length() > 0) next.append('\n');
                next.append(line);
                prefs.edit().putString(PREF_RELAY_DIAGNOSTICS, next.toString()).apply();
            } catch (Exception ignored) {}
        }
    }

    private void cancelReconnect() {
        synchronized (reconnectLock) {
            if (reconnectRunnable != null && mainHandler != null) {
                mainHandler.removeCallbacks(reconnectRunnable);
            }
            reconnectRunnable = null;
            reconnectDueAt = 0;
        }
    }

    /** 安排唯一的重连任务；网络恢复可以把已有退避任务提前到现在。 */
    private boolean scheduleReconnect(String reason, long delayMs) {
        if (!shouldRun || serverUrl == null || wsConnected.get() || mainHandler == null) return false;
        long delay = Math.max(0, Math.min(delayMs, (long) MAX_RECONNECT_DELAY));
        // Handler.postAtTime 使用 uptimeMillis 时钟，不能混用 elapsedRealtime。
        long dueAt = SystemClock.uptimeMillis() + delay;
        synchronized (reconnectLock) {
            if (!shouldRun || wsConnected.get()) return false;
            if (reconnectRunnable != null && reconnectDueAt <= dueAt) return false;
            if (reconnectRunnable != null) mainHandler.removeCallbacks(reconnectRunnable);

            final String reconnectReason = reason == null ? "unknown" : reason;
            final Runnable[] taskHolder = new Runnable[1];
            final Runnable task = () -> {
                synchronized (reconnectLock) {
                    if (reconnectRunnable != taskHolder[0]) return;
                    reconnectRunnable = null;
                    reconnectDueAt = 0;
                }
                if (!shouldRun || wsConnected.get()) return;
                recordRelayDiagnostic("reconnect_attempt", reconnectReason);
                connectWebSocket();
            };
            taskHolder[0] = task;
            reconnectRunnable = task;
            reconnectDueAt = dueAt;
            mainHandler.postAtTime(task, dueAt);
        }
        recordRelayDiagnostic("reconnect_scheduled", reason + " in=" + delay + "ms");
        return true;
    }

    private void scheduleReconnectWithBackoff(String reason) {
        int delay = Math.max(BASE_RECONNECT_DELAY, reconnectDelay);
        if (scheduleReconnect(reason, delay)) {
            reconnectDelay = Math.min(delay * 2, MAX_RECONNECT_DELAY);
        }
    }

    private synchronized void connectWebSocket() {
        if (!shouldRun || wsConnected.get() || serverUrl == null || serverUrl.isEmpty()) return;
        if (!wsConnecting.compareAndSet(false, true)) return;
        cancelReconnect();

        if (wsUrl == null) {
            wsUrl = serverUrl
                .replace("http://", "ws://")
                .replace("https://", "wss://");
            if (wsUrl.endsWith("/")) wsUrl = wsUrl.substring(0, wsUrl.length() - 1);
            wsUrl += "/ws/wander?platform=mobile&client_kind=native_service&client=native_service";
        }

        final int gen = wsGeneration.incrementAndGet();
        WebSocket old = webSocket;
        webSocket = null;
        if (old != null) {
            try { old.close(1000, "reconnect"); } catch (Exception ignored) {}
        }

        Request req = new Request.Builder().url(wsUrl).build();
        Log.i(TAG, "连接 WebSocket");
        recordRelayDiagnostic("connect_start", "generation=" + gen);

        webSocket = client.newWebSocket(req, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket ws, okhttp3.Response response) {
                if (gen != wsGeneration.get()) { ws.close(1000, "stale"); return; }
                wsConnecting.set(false);
                wsConnected.set(true);
                cancelReconnect();
                reconnectDelay = BASE_RECONNECT_DELAY;
                BackendEndpointPool.recordConnected(MirrowRelayService.this, serverUrl);
                long downtime = lastDisconnectTime > 0
                    ? System.currentTimeMillis() - lastDisconnectTime : 0;
                lastMessageTime = System.currentTimeMillis();
                RelayWatchdogStore.wsOpened(MirrowRelayService.this);
                updateKeepAlive("WS 已连接");
                Log.i(TAG, "WebSocket 已连接");
                recordRelayDiagnostic("connected", downtime > 0 ? "downtime=" + downtime + "ms" : "initial");
                sendRelayHello(ws);
                mainHandler.post(notificationChangedSender);
                JSONObject wifiSnapshot = getCurrentWifiInfo();
                // 断线期间由本机 NetworkCallback 真实观察到的变化可以补交；没有证据时
                // 仍只同步 snapshot，绝不把一次普通重连伪装成“刚到家”。
                if (!flushPendingWifiTransition(wifiSnapshot)) {
                    sendWifiEvent(
                        wifiSnapshot.optBoolean("connected", false),
                        wifiSnapshot.optString("ssid", ""),
                        wifiSnapshot.optString("bssid", ""),
                        "snapshot"
                    );
                }
                flushCheckupEvents();
                if (ambientAudio != null) ambientAudio.onConnected();
            }

            @Override
            public void onMessage(WebSocket ws, String text) {
                if (gen != wsGeneration.get()) return;
                lastMessageTime = System.currentTimeMillis();
                handleMessage(text);
            }

            @Override
            public void onFailure(WebSocket ws, Throwable t, okhttp3.Response response) {
                if (gen != wsGeneration.get()) return;
                wsConnecting.set(false);
                wsConnected.set(false);
                lastDisconnectTime = System.currentTimeMillis();
                // 不把异常原文写入持久化日志：OkHttp 的网络异常可能带本机/服务端 IP。
                String cause = t == null ? "unknown" : t.getClass().getSimpleName();
                if (response != null) cause += " http=" + response.code();
                String nextUrl = BackendEndpointPool.rotateAfterFailure(
                    MirrowRelayService.this, serverUrl
                );
                if (nextUrl != null && !nextUrl.equals(serverUrl)) {
                    serverUrl = nextUrl;
                    wsUrl = null;
                }
                RelayWatchdogController.onDisconnected(MirrowRelayService.this, cause);
                scheduleReconnectWithBackoff("failure " + cause);
                Log.w(TAG, "WebSocket 断开，已安排重连: " + cause);
                recordRelayDiagnostic("failure", cause);
                updateKeepAlive("WS 已断开 · 正在重连");
            }

            @Override
            public void onClosed(WebSocket ws, int code, String reason) {
                if (gen != wsGeneration.get()) return;
                wsConnecting.set(false);
                wsConnected.set(false);
                lastDisconnectTime = System.currentTimeMillis();
                RelayWatchdogController.onDisconnected(MirrowRelayService.this, "closed_" + code);
                scheduleReconnectWithBackoff("closed code=" + code + " reason=" + reason);
                recordRelayDiagnostic("closed", "code=" + code + " reason=" + reason);
                updateKeepAlive("WS 已断开 · 正在重连");
            }
        });
    }

    private synchronized void disconnectWebSocket() {
        wsConnected.set(false);
        wsConnecting.set(false);
        wsGeneration.incrementAndGet();
        WebSocket ws = webSocket;
        webSocket = null;
        if (ws != null) {
            try { ws.close(1000, "service stop"); } catch (Exception ignored) {}
        }
    }

    /** Repairs a stale relay through the existing reconnect state machine. */
    private void repairRelay(String reason) {
        long lastAck = RelayWatchdogStore.lastServerAckAt(this);
        boolean fresh = wsConnected.get() && lastAck > 0
            && System.currentTimeMillis() - lastAck <= HEALTH_TIMEOUT;
        if (fresh) {
            flushCheckupEvents();
            return;
        }
        recordRelayDiagnostic("repair", reason);
        RelayWatchdogController.onDisconnected(this, "repair_" + reason);
        disconnectWebSocket();
        scheduleReconnect("repair_" + reason, 0);
    }

    // ================================================================
    // 心跳
    // ================================================================

    private void startHeartbeatThread() {
        heartbeatThread = new Thread(() -> {
            if (!wsConnected.get()) connectWebSocket();
            while (shouldRun) {
                try {
                    Thread.sleep(HEARTBEAT_MS);
                    if (!shouldRun) break;
                    // 部分 OEM 在 WiFi 恢复时只更新 capabilities，不重新触发 onAvailable。
                    // 心跳低频对账只在真实状态变化时上报，补齐后台漏事件而不制造重复 arrival。
                    reconcileWifiState();
                    if (wsConnected.get() && webSocket != null) {
                        boolean sent = webSocket.send("ping") && sendRelayHeartbeat();
                        if (!sent) {
                            wsConnected.set(false);
                            lastDisconnectTime = System.currentTimeMillis();
                            RelayWatchdogController.onDisconnected(MirrowRelayService.this, "heartbeat_send_failed");
                            scheduleReconnectWithBackoff("heartbeat_send_failed");
                        } else if (lastMessageTime > 0
                            && System.currentTimeMillis() - lastMessageTime > HEALTH_TIMEOUT) {
                            wsConnected.set(false);
                            lastDisconnectTime = System.currentTimeMillis();
                            RelayWatchdogController.onDisconnected(MirrowRelayService.this, "health_timeout");
                            scheduleReconnectWithBackoff("health_timeout");
                        }
                    } else if (!wsConnected.get()) {
                        scheduleReconnect("heartbeat_disconnected", reconnectDelay);
                    }
                } catch (InterruptedException e) {
                    break;
                } catch (Exception e) {
                    Log.w(TAG, "心跳异常: " + e.getMessage());
                }
            }
        }, "MirrowHeartbeat");
        heartbeatThread.setDaemon(false);
        heartbeatThread.start();
    }

    private void stopHeartbeatThread() {
        if (heartbeatThread != null) {
            heartbeatThread.interrupt();
            try { heartbeatThread.join(1000); } catch (InterruptedException ignored) {}
        }
    }

    // ================================================================
    // 消息处理 — relay 协议
    // ================================================================

    private void handleMessage(String text) {
        // 文本 ping/pong（后端 Wander WS 用纯文本心跳）
        if ("ping".equals(text)) {
            if (webSocket != null && wsConnected.get()) {
                webSocket.send("pong");
            }
            RelayWatchdogController.onServerAcknowledged(this);
            return;
        }
        if ("pong".equals(text)) {
            RelayWatchdogController.onServerAcknowledged(this);
            return;
        }
        try {
            JSONObject json = new JSONObject(text);
            String type = json.optString("type", "");
            if ("wander_status".equals(type)) {
                mainHandler.post(com.mirrow.app.widget.WanderWidget::wake);
            } else if ("relay_hello_ack".equals(type) || "relay_heartbeat_ack".equals(type)) {
                RelayWatchdogController.onServerAcknowledged(this);
            } else if ("ambient_listening_config".equals(type)) {
                if (ambientAudio != null) ambientAudio.updateConfig(json);
            } else if ("ambient_audio_ack".equals(type)) {
                if (ambientAudio != null) ambientAudio.onAck(json);
            } else if ("checkup_alarm_event_ack".equals(type)) {
                CheckupAlarmController.ackEvent(this, json.optString("event_key", ""));
            } else if ("mobile_tool_request".equals(type)) {
                String requestId = json.optString("request_id", "");
                String tool = json.optString("tool", "");
                JSONObject params = json.optJSONObject("params");
                if (params == null) params = new JSONObject();
                handleToolRequest(requestId, tool, params);
            }
        } catch (Exception e) {
            Log.w(TAG, "消息解析失败: " + e.getMessage());
        }
    }

    private void sendRelayHello(WebSocket ws) {
        try {
            JSONObject hello = new JSONObject();
            hello.put("type", "relay_hello");
            hello.put("client_kind", "native_service");
            hello.put("service_instance_id", serviceInstanceId);
            org.json.JSONArray capabilities = new org.json.JSONArray();
            capabilities.put("tools");
            capabilities.put("telemetry");
            capabilities.put("notifications");
            if (ambientAudio != null) {
                capabilities.put("ambient_source_lease_v1");
            }
            if (ambientAudio != null && ambientAudio.isArmed()) {
                capabilities.put("ambient_audio_capture");
            }
            // 具体工具能力用于后端选路；未列出的工具仍可由兼容的 WebView relay 回退。
            String[] nativeTools = {
                    "mobile_current_app",
                    "mobile_notifications",
                    "mobile_steps_sensor",
                    "mobile_sensor_context",
                    "mobile_usage",
                    "mobile_screenshot",
                    "mobile_wifi_status",
                    "mobile_location",
                    "mobile_heart_rate",
                    "mobile_miband",
                    "mobile_ble_scan",
                    "mobile_notify",
                    "mobile_alarm",
                    "mobile_alarm_cancel",
                    "mobile_battery",
                    "mobile_toy2",
                    "mobile_open_netease",
                    "mobile_music_control",
                    "mobile_now_playing"
            };
            for (String nativeTool : nativeTools) {
                capabilities.put("tool:" + nativeTool);
            }
            // Do not claim this after boot/process death merely because the permission is
            // persisted. It is eligible only after a visible Activity has armed this process.
            if (isBackgroundCameraReady()) {
                capabilities.put("tool:mobile_camera");
                cameraCapabilityAdvertised = true;
            } else {
                cameraCapabilityAdvertised = false;
            }
            hello.put("capabilities", capabilities);
            ws.send(hello.toString());
        } catch (Exception e) {
            Log.w(TAG, "relay hello failed: " + e.getMessage());
        }
    }

    private boolean sendRelayHeartbeat() {
        try {
            refreshBackgroundCameraValidity();
            WebSocket ws = webSocket;
            if (ws == null || !wsConnected.get()) return false;
            JSONObject heartbeat = new JSONObject();
            heartbeat.put("type", "relay_heartbeat");
            heartbeat.put("service_instance_id", serviceInstanceId);
            heartbeat.put("client_kind", "native_service");
            return ws.send(heartbeat.toString());
        } catch (Exception e) {
            return false;
        }
    }

    private void handleToolRequest(String requestId, String tool, JSONObject params) {
        if (tool != null && tool.startsWith("mobile_")) {
            pendingDataTools.put(requestId == null ? "" : requestId, tool);
            RelayWatchdogStore.dataRequested(this);
            try {
                JSONObject fields = relayObservationState();
                fields.put("tool", tool);
                fields.put("request_id", shortRequestId(requestId));
                recordRelayObservation("data_request", fields);
            } catch (Exception ignored) {}
        }
        JSONObject result = new JSONObject();
        try {
            switch (tool) {
                case "mobile_miband": {
                    new Thread(() -> {
                        try {
                            JSONObject band = com.mirrow.app.miband.MiBandBridge.execute(this, params);
                            sendRelayResult(requestId, band.optBoolean("success", false), band);
                        } catch (Exception e) {
                            JSONObject failure = new JSONObject();
                            try { failure.put("success", false).put("error", "手环操作失败：" + e.getMessage()); } catch (Exception ignored) {}
                            sendRelayResult(requestId, false, failure);
                        }
                    }, "mirrow-band-relay").start();
                    break;
                }
                case "mobile_current_app":
                    result.put("data", getCurrentAppData());
                    sendRelayResult(requestId, true, result);
                    break;
                case "mobile_notifications": {
                    // 通用通知读取：从 NotificationListener 缓存读（用户已授权"通知访问"，后台亦可用）
                    // 2026-08-24：透传 category/ongoing/count（噪声已在缓存层过滤）
                    String[][] notifs = MirrowNotificationListener.getRecentNotifications();
                    if (notifs == null) throw new IllegalStateException("通知监听服务未连接或当前快照不可用");
                    org.json.JSONArray arr = new org.json.JSONArray();
                    if (notifs != null) {
                        for (String[] n : notifs) {
                            org.json.JSONObject o = new org.json.JSONObject();
                            try {
                                o.put("package", n[0]);
                                o.put("title", n[1]);
                                o.put("text", n[2]);
                                o.put("time", n[3]);
                                o.put("category", n.length > 4 ? n[4] : "");
                                o.put("ongoing", n.length > 5 ? n[5] : "0");
                                o.put("count", n.length > 6 ? Integer.parseInt(n[6]) : 1);
                                o.put("active", true);
                            } catch (Exception ignored) {}
                            arr.put(o);
                        }
                    }
                    result.put("data", arr);
                    result.put("snapshot_scope", "active_notifications");
                    result.put("observed_at_ms", System.currentTimeMillis());
                    result.put("content", "通知数: " + arr.length());
                    result.put("success", true);
                    Log.i(TAG, "mobile_notifications: " + arr.length() + " 条 (后台)");
                    // ⚠️ 2026-08-24 修复：缺 sendRelayResult 导致响应从不回传 → 后端哨兵通知恒空
                    sendRelayResult(requestId, true, result);
                    break;
                }
                case "mobile_steps_sensor":
                    result.put("data", getStepsData());
                    sendRelayResult(requestId, true, result);
                    break;
                case "mobile_sensor_context": {
                    // 手机姿态/运动/光线（2026-08-24 AionsHome 移植）：常驻缓存 snapshot
                    org.json.JSONObject snap = sensorClassifier.snapshot(System.currentTimeMillis());
                    result.put("data", snap);
                    result.put("success", true);
                    Log.i(TAG, "mobile_sensor_context: posture=" + snap.optString("posture", "-")
                        + " motion=" + snap.optString("motion", "-")
                        + " light=" + snap.optString("light", "-"));
                    // ⚠️ 缺 sendRelayResult → 后端哨兵收不到（同 mobile_notifications 教训）
                    sendRelayResult(requestId, true, result);
                    break;
                }
                case "mobile_usage":
                    result.put("data", getUsageData());
                    sendRelayResult(requestId, true, result);
                    break;
                case "mobile_screenshot": {
                    // 无障碍截图（takeScreenshot）——app 切后台也能截
                    String img = null;
                    String method = "";
                    if (MirrowAccessibilityService.isRunning()) {
                        try {
                            // ⚡ 短暂延迟刷新系统 framebuffer（防 ColorOS 缓存）
                            Thread.sleep(50);
                            img = MirrowAccessibilityService.getInstance()
                                .captureScreenshotBase64(1080, 82, 4000);
                            // ⚡ 去重检测：内容感知 hash（前/中/尾多点采样，避开 JPEG 固定文件头——
                            //   旧实现取前 200 字符落固定头区，不同内容也判同 → 每次截图都被当"缓存"）
                            if (img != null && img.length() > 100) {
                                String currentHash = MirrowAccessibilityService.screenshotContentHash(img);
                                if (currentHash.equals(_lastScreenshotHashBg)) {
                                    Log.w(TAG, "a11y screenshot 疑似缓存 (hash=" + currentHash + ")，重试一次");
                                    Thread.sleep(150);
                                    String retry = MirrowAccessibilityService.getInstance()
                                        .captureScreenshotBase64(1080, 82, 4000);
                                    if (retry != null && retry.length() > 100
                                            && !MirrowAccessibilityService.screenshotContentHash(retry)
                                                .equals(_lastScreenshotHashBg)) {
                                        img = retry;  // 重试拿到新帧
                                        currentHash = MirrowAccessibilityService.screenshotContentHash(img);
                                    } else {
                                        img = null;  // 重试仍缓存 → 报告截图不可用
                                    }
                                }
                                _lastScreenshotHashBg = currentHash;
                            }
                            if (img != null && img.length() > 100) method = "a11y";
                        } catch (Throwable t) {
                            Log.w(TAG, "a11y screenshot failed: " + t.getMessage());
                        }
                    }
                    if (img != null && img.length() > 100) {
                        result.put("image_base64", img);
                        result.put("content", "[SS:" + method + "] OK (" + img.length() + " chars)");
                        result.put("success", true);
                        sendRelayResult(requestId, true, result);
                    } else {
                        result.put("image_base64", (Object) null);
                        String err = MirrowAccessibilityService.getLastShotError();
                        String detail = err.isEmpty() ? "" : " (a11y=" + err + ")";
                        result.put("content", (MirrowAccessibilityService.isRunning()
                            ? "无障碍截图失败（可能返回了缓存帧）" : "无障碍服务未开启（设置→无障碍→MIRROW 屏幕感知）") + detail);
                        result.put("success", false);
                        sendRelayResult(requestId, false, result);
                    }
                    break;
                }
                case "mobile_camera": {
                    if (!isBackgroundCameraReady()) {
                        // Permission may have been revoked after relay_hello. Remove the
                        // capability immediately rather than pretending this WebView-only path works.
                        if (!hasCameraPermission()) disarmBackgroundCamera("permission_revoked");
                        result.put("data", new JSONObject().put("available", false)
                                .put("error", "background_camera_not_armed"));
                        result.put("content", "手机后台相机尚未在前台完成启用；请打开 MIRROW 后重试");
                        sendRelayResult(requestId, false, result);
                        break;
                    }
                    final String facing = params.optString("facing", "back");
                    final JSONObject cameraResult = new JSONObject();
                    // analysis_prompt is intentionally a server-side concern; the native relay
                    // captures only the JPEG and reports an exact transport result.
                    backgroundCamera.capture(facing, new BackgroundCameraController.Callback() {
                        @Override public void onSuccess(byte[] jpeg, JSONObject metadata) {
                            try {
                                cameraResult.put("image_base64", android.util.Base64.encodeToString(
                                        jpeg, android.util.Base64.NO_WRAP));
                                cameraResult.put("data", metadata);
                                cameraResult.put("content", "[BG:CAM] OK (" + jpeg.length + " bytes)");
                                sendRelayResult(requestId, true, cameraResult);
                            } catch (Exception e) {
                                try {
                                    cameraResult.put("content", "后台相机结果编码失败");
                                    sendRelayResult(requestId, false, cameraResult);
                                } catch (Exception ignored) {}
                            }
                        }
                        @Override public void onFailure(String error) {
                            try {
                                cameraResult.put("data", new JSONObject().put("available", false)
                                        .put("error", error));
                                cameraResult.put("content", "后台相机拍摄失败: " + error);
                                sendRelayResult(requestId, false, cameraResult);
                            } catch (Exception ignored) {}
                        }
                    });
                    break;
                }
                case "mobile_wifi_status":
                    result.put("data", getCurrentWifiInfo());
                    sendRelayResult(requestId, true, result);
                    break;
                case "mobile_location":
                    if (lastGpsTime > 0) {
                        JSONObject loc = new JSONObject();
                        loc.put("lat", lastLat);
                        loc.put("lng", lastLng);
                        result.put("data", loc);
                        sendRelayResult(requestId, true, result);
                    } else {
                        // 无实时定位 → 取最后一次已知位置（网络/基站缓存）
                        Location cached = null;
                        try {
                            cached = locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
                            if (cached == null)
                                cached = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER);
                        } catch (SecurityException ignored) {}
                        if (cached != null) {
                            lastLat = cached.getLatitude();
                            lastLng = cached.getLongitude();
                            lastGpsTime = cached.getTime();
                            JSONObject loc = new JSONObject();
                            loc.put("lat", lastLat);
                            loc.put("lng", lastLng);
                            result.put("data", loc);
                            sendRelayResult(requestId, true, result);
                            Log.d(TAG, "GPS(fallback): " + lastLat + "," + lastLng);
                        } else {
                            result.put("error", "GPS 暂无定位");
                            sendRelayResult(requestId, false, result);
                        }
                    }
                    break;
                case "mobile_heart_rate": {
                    JSONObject hr = readHeartRateBle(params.optString("mac", HR_MAC));
                    boolean ok = hr.optBoolean("_success", false);
                    hr.remove("_success");
                    if (ok) {
                        result.put("data", hr);
                        result.put("content", "心率: " + hr.optInt("heart_rate") + " bpm (原生后台)");
                        sendRelayResult(requestId, true, result);
                    } else {
                        result.put("content", hr.optString("_error", "[BLE] 心率读取失败"));
                        sendRelayResult(requestId, false, result);
                    }
                    break;
                }
                case "mobile_ble_scan": {
                    JSONObject scan = listBondedBleDevices();
                    boolean sok = scan.optBoolean("_success", false);
                    scan.remove("_success");
                    if (sok) {
                        org.json.JSONArray devs = scan.optJSONArray("devices");
                        result.put("data", scan);
                        result.put("content", "已配对设备 " + (devs != null ? devs.length() : 0) + " 个");
                        sendRelayResult(requestId, true, result);
                    } else {
                        result.put("content", scan.optString("_error", "[BLE] 扫描失败"));
                        sendRelayResult(requestId, false, result);
                    }
                    break;
                }
                case "mobile_notify": {
                    String title = params.optString("title", "MIRROW");
                    String body = params.optString("body", "AI 发来了一条消息");
                    String deliveryId = params.optString("delivery_id", "");

                    // 检查通知权限 (Android 13+)
                    if (Build.VERSION.SDK_INT >= 33) {
                        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                                != PackageManager.PERMISSION_GRANTED) {
                            result.put("content", "通知权限未授予");
                            result.put("success", false);
                            sendRelayResult(requestId, false, result);
                            break;
                        }
                    }

                    // 创建通知渠道 (Android 8+) — channel ID "mirrow_push" 与 MobileToolPlugin 一致
                    NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                    if (Build.VERSION.SDK_INT >= 26 && nm != null) {
                        NotificationChannel channel = new NotificationChannel(
                                "mirrow_push", "MIRROW消息",
                                NotificationManager.IMPORTANCE_HIGH);
                        channel.setDescription("AI 的主动通知");
                        nm.createNotificationChannel(channel);
                    }

                    // 构建点击跳转 Intent
                    Intent launchIntent = getPackageManager()
                            .getLaunchIntentForPackage(getPackageName());
                    if (launchIntent == null) {
                        launchIntent = new Intent(this, MainActivity.class);
                    }
                    launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                    PendingIntent pendingIntent = PendingIntent.getActivity(
                            this, 0, launchIntent,
                            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

                    // 构建并发送通知
                    Notification notif = new Notification.Builder(this, "mirrow_push")
                            .setSmallIcon(android.R.drawable.ic_dialog_info)
                            .setContentTitle(title)
                            .setContentText(body)
                            .setContentIntent(pendingIntent)
                            .setPriority(Notification.PRIORITY_HIGH)
                            .setCategory(Notification.CATEGORY_MESSAGE)
                            .setDefaults(Notification.DEFAULT_SOUND | Notification.DEFAULT_VIBRATE)
                            .setAutoCancel(true)
                            .build();

                    int notificationId = deliveryId.isEmpty()
                            ? (int) (System.currentTimeMillis() & 0x7FFFFFFF)
                            : (deliveryId.hashCode() & 0x7FFFFFFF);
                    // 同一 delivery_id 重试时更新原通知，避免断线重连造成重复弹窗。
                    nm.notify("mirrow_delivery", notificationId, notif);

                    Log.i(TAG, "mobile_notify: sent notification title=" + title);
                    result.put("content", "通知已发送: " + title);
                    result.put("success", true);
                    sendRelayResult(requestId, true, result);
                    break;
                }
                case "mobile_alarm": {
                    JSONObject alarmResult = CheckupAlarmController.schedule(this, params);
                    sendRelayResult(requestId, alarmResult.optBoolean("success", false), alarmResult);
                    break;
                }
                case "mobile_alarm_cancel": {
                    JSONObject alarmResult = CheckupAlarmController.cancel(this, params);
                    sendRelayResult(requestId, alarmResult.optBoolean("success", false), alarmResult);
                    break;
                }
                case "mobile_battery":
                    result.put("data", getBatteryData());
                    sendRelayResult(requestId, true, result);
                    break;
                case "mobile_toy2": {
                    // BLE can wait for discovery and several writes. Keep the
                    // relay WebSocket callback free so heartbeat/reconnect works.
                    new Thread(() -> {
                        JSONObject toyResult = new JSONObject();
                        String action = params.optString("action", "stretch");
                        int mode = params.optInt("mode", 1);
                        int intensity = params.optInt("intensity", 0);
                        boolean heat = params.optBoolean("heat", false);
                        int vibrateIntensity = params.optInt("vibrate_intensity", 0);
                        try {
                            String mac = params.optString("mac", null);
                            if (mac != null && !mac.isEmpty()) BLEController.setTianyanMac(mac);
                            String bleJson = BLEController.getInstance(this).tianyanApply(
                                    action, mode, intensity, heat, vibrateIntensity);
                            JSONObject parsed = new JSONObject(bleJson);
                            toyResult.put("success", parsed.optBoolean("success", false));
                            toyResult.put("content", parsed.optString("content",
                                    parsed.optString("error", "BLE error")));
                        } catch (Exception e) {
                            Log.e(TAG, "mobile_toy2 BLE error", e);
                            try { toyResult.put("success", false).put("content", "BLE error: " + e.getClass().getSimpleName()); }
                            catch (Exception ignored) {}
                        }
                        MainApplication.log("TOY2", action + " m=" + mode + " i=" + intensity
                                + (heat ? " +heat" : "") + " → " + toyResult.optString("content"), null);
                        sendRelayResult(requestId, toyResult.optBoolean("success", false), toyResult);
                    }, "mirrow-toy2-relay").start();
                    break;
                }
                case "mobile_open_netease":
                case "mobile_music_control": {
                    if ("mobile_open_netease".equals(tool) && !params.has("action")) {
                        params.put("action", "open");
                    }
                    result = NeteaseMusicController.execute(this, params);
                    sendRelayResult(requestId, result.optBoolean("success", false), result);
                    break;
                }
                case "mobile_now_playing":
                    result.put("data", getNowPlayingFromNotification());
                    sendRelayResult(requestId, true, result);
                    break;
                default:
                    result.put("error", "unknown tool: " + tool);
                    sendRelayResult(requestId, false, result);
            }
        } catch (Exception e) {
            try {
                result.put("error", e.getMessage());
                sendRelayResult(requestId, false, result);
            } catch (Exception ignored) {}
        }
    }

    private void sendRelayResult(String requestId, boolean success, JSONObject result) {
        String tool = pendingDataTools.remove(requestId == null ? "" : requestId);
        boolean sent = false;
        try {
            // 内嵌 success 到 result 对象——后端 _dynamic_relay_sender 检查 result["success"]
            try { result.put("success", success); } catch (Exception ignored) {}
            JSONObject msg = new JSONObject();
            msg.put("type", "mobile_tool_result");
            msg.put("request_id", requestId);
            msg.put("success", success);   // 顶层保留（兼容旧 relay 路径）
            msg.put("result", result);
            if (webSocket != null && wsConnected.get()) {
                sent = webSocket.send(msg.toString());
            }
            if (tool != null) recordDataResult(tool, requestId, success, sent);
        } catch (Exception e) {
            if (tool != null) recordDataResult(tool, requestId, success, false);
            Log.w(TAG, "发送 relay 结果失败: " + e.getMessage());
        }
    }

    private void recordDataResult(String tool, String requestId, boolean success, boolean sent) {
        RelayWatchdogStore.dataResult(this);
        try {
            JSONObject fields = relayObservationState();
            fields.put("tool", tool == null ? "" : tool);
            fields.put("request_id", shortRequestId(requestId));
            fields.put("success", success);
            fields.put("sent", sent);
            recordRelayObservation("data_result", fields);
        } catch (Exception ignored) {}
    }

    /** 发送手机本地积压的查岗闹钟状态；事件只有收到后端 ACK 才会从本地队列移除。 */
    private void flushCheckupEvents() {
        CheckupAlarmController.flushPendingEvents(this, message -> {
            try {
                WebSocket ws = webSocket;
                return ws != null && wsConnected.get() && ws.send(message.toString());
            } catch (Exception e) {
                return false;
            }
        });
    }

    // ── WiFi 事件推送 ──────────────────────────────────

    private void sendWifiEvent(boolean connected, String ssid, String bssid) {
        sendWifiEvent(connected, ssid, bssid, "transition");
    }

    private void sendWifiEvent(boolean connected, String ssid, String bssid, String eventKind) {
        try {
            if ("transition".equals(eventKind)
                    && (webSocket == null || !wsConnected.get())) {
                rememberPendingWifiTransition(connected, ssid, bssid);
                return;
            }
            JSONObject msg = new JSONObject();
            msg.put("type", "wifi_state_change");
            msg.put("event_kind", eventKind);
            msg.put("device_id", android.provider.Settings.Secure.getString(
                getContentResolver(), android.provider.Settings.Secure.ANDROID_ID));
            msg.put("device_model", android.os.Build.MANUFACTURER + " " + android.os.Build.MODEL);
            msg.put("connected", connected);
            if (connected) {
                msg.put("ssid", ssid != null ? ssid : "");
                msg.put("bssid", bssid != null ? bssid : "");
            } else {
                msg.put("previous_ssid", currentWifiSsid != null ? currentWifiSsid : "");
            }
            msg.put("timestamp", System.currentTimeMillis());
            if (webSocket != null && wsConnected.get()) {
                boolean accepted = webSocket.send(msg.toString());
                if (accepted) {
                    if ("transition".equals(eventKind)) clearPendingWifiTransition();
                    Log.i(TAG, "WiFi event pushed: connected=" + connected + " (network id omitted)");
                } else if ("transition".equals(eventKind)) {
                    rememberPendingWifiTransition(connected, ssid, bssid);
                }
            }
        } catch (Exception e) {
            if ("transition".equals(eventKind)) {
                rememberPendingWifiTransition(connected, ssid, bssid);
            }
            Log.w(TAG, "发送 WiFi 事件失败: " + e.getMessage());
        }
    }

    private void rememberPendingWifiTransition(boolean connected, String ssid, String bssid) {
        try {
            JSONObject pending = new JSONObject();
            pending.put("connected", connected);
            pending.put("ssid", ssid != null ? ssid : "");
            pending.put("bssid", bssid != null ? bssid : "");
            pending.put("at", System.currentTimeMillis());
            prefs.edit().putString(PREF_WIFI_PENDING, pending.toString()).apply();
        } catch (Exception e) {
            Log.w(TAG, "暂存 WiFi 变化失败: " + e.getMessage());
        }
    }

    private void clearPendingWifiTransition() {
        try { prefs.edit().remove(PREF_WIFI_PENDING).apply(); }
        catch (Exception ignored) {}
    }

    private boolean flushPendingWifiTransition(JSONObject current) {
        try {
            String raw = prefs.getString(PREF_WIFI_PENDING, "");
            if (raw == null || raw.isEmpty()) return false;
            JSONObject pending = new JSONObject(raw);
            long age = System.currentTimeMillis() - pending.optLong("at", 0L);
            boolean connected = pending.optBoolean("connected", false);
            boolean currentConnected = current.optBoolean("connected", false);
            String pendingSsid = pending.optString("ssid", "");
            String currentSsid = current.optString("ssid", "");
            boolean stillMatches = connected == currentConnected
                && (!connected || pendingSsid.equals(currentSsid));
            if (age < 0 || age > WIFI_PENDING_MAX_AGE_MS || !stillMatches) {
                clearPendingWifiTransition();
                return false;
            }
            sendWifiEvent(
                connected,
                pendingSsid,
                pending.optString("bssid", ""),
                "transition"
            );
            Log.i(TAG, "Replayed pending WiFi transition (network id omitted)");
            return true;
        } catch (Exception e) {
            clearPendingWifiTransition();
            return false;
        }
    }

    private void onWifiConnected(String ssid, String bssid) {
        final String newSsid = ssid != null ? ssid : "";
        final String newBssid = bssid != null ? bssid : "";
        // 去抖动：SSID 和 BSSID 都没变 → 不重复推送
        if (currentWifiConnected && newSsid.equals(currentWifiSsid) && newBssid.equals(currentWifiBssid)) {
            return;
        }
        currentWifiSsid = newSsid;
        currentWifiBssid = newBssid;
        currentWifiConnected = true;
        lastWifiEventTime = System.currentTimeMillis();

        // 去抖动：3s 内不重复推送（防快速断开重连）
        if (pendingWifiDebounce != null) {
            mainHandler.removeCallbacks(pendingWifiDebounce);
            pendingWifiDebounce = null;
        }
        sendWifiEvent(true, currentWifiSsid, currentWifiBssid);
    }

    private void onWifiDisconnected() {
        // 去抖动：延迟 3s 确认不是暂时断开
        if (pendingWifiDebounce != null) {
            mainHandler.removeCallbacks(pendingWifiDebounce);
        }
        pendingWifiDebounce = () -> {
            if (!currentWifiConnected) {
                // 确实断开了（没有被 reconnect 取消）
                sendWifiEvent(false, null, null);
            }
            pendingWifiDebounce = null;
        };
        mainHandler.postDelayed(pendingWifiDebounce, WIFI_DEBOUNCE_MS);
        currentWifiConnected = false;
        currentWifiSsid = null;
        currentWifiBssid = null;
        lastWifiEventTime = System.currentTimeMillis();
    }

    private void seedCurrentWifiState() {
        // 初始状态只是基线，不是 arrival。回调注册前先播种本地比较状态，
        // 避免服务重启把当前已连接状态伪装成一次新变化。
        JSONObject info = getCurrentWifiInfo();
        currentWifiConnected = info.optBoolean("connected", false);
        currentWifiSsid = currentWifiConnected ? info.optString("ssid", "") : null;
        currentWifiBssid = currentWifiConnected ? info.optString("bssid", "") : null;
    }

    private void reconcileWifiState() {
        JSONObject info = getCurrentWifiInfo();
        boolean connected = info.optBoolean("connected", false);
        mainHandler.post(() -> {
            if (connected) {
                onWifiConnected(info.optString("ssid", ""), info.optString("bssid", ""));
            } else if (currentWifiConnected) {
                onWifiDisconnected();
            }
        });
    }

    private JSONObject getCurrentWifiInfo() {
        JSONObject info = new JSONObject();
        try {
            if (wifiManager == null) {
                wifiManager = (android.net.wifi.WifiManager)
                    getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            }
            if (wifiManager != null) {
                android.net.wifi.WifiInfo wi = wifiManager.getConnectionInfo();
                if (wi != null && wi.getNetworkId() != -1) {
                    String ssid = wi.getSSID();
                    String bssid = wi.getBSSID();
                    info.put("ssid", ssid != null ? ssid : "");
                    info.put("bssid", bssid != null ? bssid : "");
                    info.put("connected", true);
                    info.put("signal_strength", wi.getRssi());
                    // 判断 5G：频率 > 5000 MHz
                    int freq = wi.getFrequency();
                    info.put("is_5g", freq > 5000);
                    // IP 地址
                    int ip = wi.getIpAddress();
                    if (ip != 0) {
                        info.put("ip_address", String.format("%d.%d.%d.%d",
                            (ip & 0xff), (ip >> 8 & 0xff), (ip >> 16 & 0xff), (ip >> 24 & 0xff)));
                    }
                } else {
                    info.put("connected", false);
                    info.put("ssid", (Object) null);
                    info.put("bssid", (Object) null);
                }
            } else {
                info.put("connected", false);
                info.put("error", "WifiManager unavailable");
            }
        } catch (Exception e) {
            Log.w(TAG, "获取 WiFi 信息失败: " + e.getMessage());
            try { info.put("connected", false); info.put("error", e.getMessage()); } catch (Exception ignored) {}
        }
        return info;
    }

    // ================================================================
    // MediaSession 监听 — 音乐耳蜗
    // ================================================================

    // ── 音乐耳蜗：通知栏轮询 + HTTP POST（无 WebSocket 依赖）──
    private void startMusicPollThread() {
        musicPollThread = new Thread(() -> {
            Log.i(TAG, "音乐耳蜗轮询已启动 (" + (MUSIC_POLL_MS / 1000) + "s)");
            while (shouldRun) {
                try {
                    Thread.sleep(MUSIC_POLL_MS);
                    if (!shouldRun) break;
                    JSONObject info = getNowPlayingFromNotification();
                    String title = info.optString("title", "");
                    String artist = info.optString("artist", "");
                    if (title.isEmpty()) continue;
                    String key = title.trim() + "|" + artist.trim() + "|" + info.optBoolean("playing", false);
                    if (key.equals(lastPostedSongKey)) continue; // 去重
                    lastPostedSongKey = key;
                    postNowPlaying(info);
                } catch (InterruptedException e) {
                    break;
                } catch (Exception e) {
                    Log.w(TAG, "音乐轮询异常: " + e.getMessage());
                }
            }
            Log.i(TAG, "音乐耳蜗轮询已停止");
        }, "MirrowMusicPoll");
        musicPollThread.setDaemon(true);
        musicPollThread.start();
    }

    private void postNowPlaying(JSONObject body) {
        try {
            okhttp3.RequestBody rb = okhttp3.RequestBody.create(
                body.toString(), okhttp3.MediaType.get("application/json"));
            okhttp3.Request req = new okhttp3.Request.Builder()
                .url(serverUrl + "/api/music/cochlea/now-playing")
                .post(rb)
                .build();
            client.newCall(req).enqueue(new okhttp3.Callback() {
                @Override public void onFailure(okhttp3.Call call, java.io.IOException e) {
                    Log.w(TAG, "音乐上报失败: " + e.getMessage());
                }
                @Override public void onResponse(okhttp3.Call call, okhttp3.Response response) {
                    Log.i(TAG, "🎵 音乐上报: " + body.optString("title") + " - "
                        + body.optString("artist") + " (" + response.code() + ")");
                    response.close();
                }
            });
        } catch (Exception e) {
            Log.w(TAG, "音乐上报异常: " + e.getMessage());
        }
    }

    // ── 音乐耳蜗 relay 工具（通过 AccessibilityService 读通知栏）──
    private JSONObject getNowPlayingFromNotification() {
        try {
            JSONObject request = new JSONObject().put("action", "now_playing");
            JSONObject controlled = NeteaseMusicController.execute(this, request);
            JSONObject mediaData = controlled.optJSONObject("data");
            if (controlled.optBoolean("success", false) && mediaData != null
                    && !mediaData.optString("title", "").isEmpty()) {
                return mediaData;
            }
        } catch (Exception ignored) {}
        JSONObject data = new JSONObject();
        try {
            String[] info = MirrowNotificationListener.getLastSong();
            if (info != null && info[0] != null && !info[0].isEmpty()) {
                data.put("title", info[0]);
                data.put("artist", info[1] != null ? info[1] : "");
                data.put("playing", true);
            }
        } catch (Exception e) {
            try { data.put("error", e.getMessage()); } catch (Exception ignored) {}
        }
        return data;
    }

    // ================================================================
    // 数据采集 — current_app
    // ================================================================

    private void startActivityThread() {
        activityThread = new Thread(() -> {
            while (shouldRun) {
                try {
                    Thread.sleep(ACTIVITY_INTERVAL);
                    if (!shouldRun) break;
                    reconcileScreenState();
                    if (screenOn) detectForegroundApp();
                    trackPhoneActivity();   // 累积手机持续活动时长
                } catch (InterruptedException e) {
                    break;
                }
            }
        }, "MirrowActivity");
        activityThread.setDaemon(true);
        activityThread.start();
    }

    /**
     * 每60s采样屏幕状态 + 前台App，本地累积连续活动秒数。
     * 累积达 15min（900s）→ 主动 push phone_activity_report 唤醒 sleeping。
     * 中断（灭屏/无App/静止超60s）→ 重置计数器 + fired 标志。
     * 纯本地逻辑，不依赖后端轮询 → 抗断连。
     */
    private void trackPhoneActivity() {
        reconcileScreenState();
        long now = System.currentTimeMillis();
        long elapsedMs = (activityLastCheckMs > 0) ? (now - activityLastCheckMs) : 0;
        activityLastCheckMs = now;

        // 判定"持续活动"：屏幕亮 + 有前台 App（非自身）
        boolean isActive = screenOn && currentAppPackage != null
            && !currentAppPackage.isEmpty()
            && !currentAppPackage.equals(getPackageName());

        if (!isActive) {
            // 中断超 60s → 重置累积
            if (elapsedMs > 60_000) {
                activityAccumulatedSeconds = 0;
                activityFired = false;
            }
            return;
        }

        // 屏幕亮+有前台App → 累加
        activityAccumulatedSeconds += Math.min(elapsedMs, ACTIVITY_INTERVAL) / 1000;

        if (!activityFired && activityAccumulatedSeconds >= ACTIVITY_FIRE_SECONDS) {
            activityFired = true;
            activityAccumulatedSeconds = 0;
            try {
                JSONObject result = new JSONObject();
                result.put("_success", true);
                result.put("event", "phone_activity_report");
                result.put("accumulated_sec", ACTIVITY_FIRE_SECONDS);
                // 用特殊的 request_id 前缀，不依赖 relay 请求-响应
                sendRelayResult("phone_activity_" + System.currentTimeMillis(), true, result);
                Log.i(TAG, "手机持续活动超15min → push phone_activity_report");
            } catch (Exception e) {
                Log.w(TAG, "phone_activity_report 推送失败: " + e.getMessage());
            }
        }
    }

    private void stopActivityThread() {
        if (activityThread != null) activityThread.interrupt();
    }

    private void detectForegroundApp() {
        try {
            // ⚡ 优先无障碍窗口事件（2026-08-23 增强：UsageEvents 有系统延迟，无障碍 TYPE_WINDOW_STATE_CHANGED 实时）。
            //   输入法属于覆盖窗口；这里取得的是其下方最近确认的内容应用。
            String a11yPkg = null;
            try {
                a11yPkg = MirrowAccessibilityService.getLastWindowPackage();
            } catch (Throwable ignored) {}
            if (a11yPkg != null && !a11yPkg.isEmpty()) {
                currentAppResolution = MirrowAccessibilityService.getForegroundOverlayPackage().isEmpty()
                    ? "accessibility_window"
                    : "underlying_app_beneath_input_method";
                currentAppObservedAtMs = MirrowAccessibilityService.getLastContentObservedAtMs();
                applyForegroundPackage(a11yPkg);
                return;
            }

            UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
            if (usm == null) return;
            long now = System.currentTimeMillis();

            // 方案一：UsageEvents (2min 窗口，60s 轮询 → 永不漏)
            String pkg = null;
            String lastObservedPkg = null;
            UsageEvents events = usm.queryEvents(now - 120_000, now);
            UsageEvents.Event event = new UsageEvents.Event();
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                if (event.getEventType() == UsageEvents.Event.ACTIVITY_RESUMED
                    || event.getEventType() == 1) {
                    lastObservedPkg = event.getPackageName();
                    if (!MirrowAccessibilityService.isInputMethodPackage(this, lastObservedPkg)) {
                        pkg = lastObservedPkg;
                    }
                }
            }

            if (pkg != null) {
                currentAppResolution = lastObservedPkg != null
                    && MirrowAccessibilityService.isInputMethodPackage(this, lastObservedPkg)
                    ? "usage_events_underlying_app_beneath_input_method"
                    : "usage_events";
                currentAppObservedAtMs = now;
            }

            // 方案二：回退 lastTimeUsed
            if (pkg == null) {
                List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 120_000, now);
                UsageStats recent = null;
                if (stats != null) {
                    for (UsageStats s : stats) {
                        if (MirrowAccessibilityService.isInputMethodPackage(this, s.getPackageName())) continue;
                        if (recent == null || s.getLastTimeUsed() > recent.getLastTimeUsed())
                            recent = s;
                    }
                }
                if (recent != null) {
                    pkg = recent.getPackageName();
                    currentAppResolution = "usage_stats_fallback";
                    currentAppObservedAtMs = recent.getLastTimeUsed();
                }
            }

            applyForegroundPackage(pkg);
        } catch (Exception e) {
            Log.w(TAG, "前台检测失败: " + e.getMessage());
        }
    }

    /** ⚡ 统一应用前台包名（2026-08-23 修复：允许 MIRROW 自己——原逻辑跳过自己包名导致识别不到）。 */
    private void applyForegroundPackage(String pkg) {
        if (pkg == null || MirrowAccessibilityService.isInputMethodPackage(this, pkg)) return;
        // 设备分类器记录前台 app（2026-08-24），供 mobile_sensor_context snapshot
        sensorClassifier.updateForegroundApp(pkg, System.currentTimeMillis());
        if (pkg.equals(getPackageName())) {
            // MIRROW 自己前台 = 人类伙伴在用 MIRROW 聊天，也要记录
            if ("com.mirrow.app".equals(currentAppPackage)) return; // unchanged
            currentAppPackage = pkg;
            currentAppName = "MIRROW";
            Log.d(TAG, "前台切换: MIRROW（自己）");
            recordRelayDiagnostic("foreground", "app=MIRROW pkg=" + pkg);
            return;
        }
        if (pkg.equals(currentAppPackage)) return; // unchanged
        currentAppPackage = pkg;
        currentAppName = resolveAppName(pkg);
        lastInteractiveAppPackage = pkg;
        lastInteractiveAppObservedAtMs = System.currentTimeMillis();
        Log.d(TAG, "前台切换: " + currentAppName + " (" + pkg + ")");
        recordRelayDiagnostic("foreground", "app=" + currentAppName + " pkg=" + pkg);
    }

    private JSONObject getCurrentAppData() {
        JSONObject data = new JSONObject();
        try {
            // 锁屏时不把熄屏前缓存的窗口误报为当前前台 App。
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            boolean screenOnLive = pm != null && pm.isInteractive();
            if (screenOnLive) {
                // 即时检测 + 始终实时解析（不信任缓存的 currentAppName）
                detectForegroundApp();
            } else {
                currentAppPackage = "";
                currentAppName = "";
                currentAppResolution = "screen_off";
                currentAppObservedAtMs = 0L;
                MirrowAccessibilityService.clearLastWindowState();
            }
            String name = resolveAppName(currentAppPackage);
            String observedWindow = MirrowAccessibilityService.getObservedWindowPackage();
            String overlayPackage = MirrowAccessibilityService.getForegroundOverlayPackage();
            data.put("package", currentAppPackage);
            data.put("app_name", (name != null && !name.isEmpty() && !name.equals(currentAppPackage)) ? name : "");
            data.put("observed_window_package", observedWindow != null ? observedWindow : "");
            data.put("overlay_package", overlayPackage != null ? overlayPackage : "");
            data.put("underlying_package", overlayPackage != null && !overlayPackage.isEmpty()
                ? currentAppPackage : "");
            data.put("foreground_resolution", currentAppResolution);
            data.put("observed_at_ms", currentAppObservedAtMs);
            // ⚠️ 2026-08-24 v3：screen_on 用 isInteractive() 而非 screenOn 广播变量——
            //   OPPO AOD/息屏显示时 SCREEN_OFF 广播可能不触发，screenOn 残留 true → 锁屏被误判"在用"。
            //   isInteractive() 在锁屏/AOD 恒为 false，亮屏在用才 true，与前台路径 MobileToolPlugin 一致。
            data.put("screen_on", screenOnLive);
            // 观测通道可用与“此刻存在前台内容 App”是两件事。熄屏是有效观测，
            // 不能被后端误当作 relay 失败并继续沿用旧的亮屏 App。
            data.put("available", true);
            data.put("foreground_available", screenOnLive && !currentAppPackage.isEmpty());
        } catch (Exception ignored) {}
        return data;
    }

    private String resolveAppName(String pkg) {
        // 优先查映射表（从 app_name_map.py 同步过来）
        Map<String, String> map = getAppNameMap();
        String name = map.get(pkg);
        if (name != null) return name;
        // 尝试系统 PackageManager
        try {
            android.content.pm.PackageManager pm = getPackageManager();
            android.content.pm.ApplicationInfo ai = pm.getApplicationInfo(pkg, 0);
            return pm.getApplicationLabel(ai).toString();
        } catch (Exception e2) {
            return pkg;
        }
    }

    // ================================================================
    // 数据采集 — 步数
    // ================================================================

    private void initSensors() {
        sensorManager = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
        if (sensorManager == null) return;
        stepSensor = sensorManager.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
        if (stepSensor != null) {
            sensorManager.registerListener(stepListener, stepSensor,
                SensorManager.SENSOR_DELAY_NORMAL, mainHandler);
        }

        // 设备姿态/运动/光线（2026-08-24 AionsHome 移植）：亮屏注册/熄屏注销
        gravitySensor = sensorManager.getDefaultSensor(Sensor.TYPE_GRAVITY);
        linearAccelSensor = sensorManager.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION);
        lightSensor = sensorManager.getDefaultSensor(Sensor.TYPE_LIGHT);
        proximitySensor = sensorManager.getDefaultSensor(Sensor.TYPE_PROXIMITY);
        if (screenOn) startForScreenOn();  // 启动时屏幕可能是亮的

        // 从持久化恢复步数状态
        stepDayStart = prefs.getFloat("step_day_start_counter", -1);
        stepRebootOffset = prefs.getFloat("step_reboot_offset", 0);
        stepLastKnown = prefs.getFloat("step_last_known_counter", -1);
        stepLogicalDate = prefs.getString("step_logical_date", "");
    }

    // ── 设备姿态传感器（2026-08-24 AionsHome PhoneContextCollector 移植）──

    private void startForScreenOn() {
        if (sensorManager == null || deviceSensorsRegistered) return;
        if (gravitySensor == null && linearAccelSensor == null) return;
        try {
            if (gravitySensor != null) sensorManager.registerListener(
                deviceSensorListener, gravitySensor, SENSOR_SAMPLE_US, SENSOR_BATCH_US);
            if (linearAccelSensor != null) sensorManager.registerListener(
                deviceSensorListener, linearAccelSensor, SENSOR_SAMPLE_US, SENSOR_BATCH_US);
            if (lightSensor != null) sensorManager.registerListener(
                deviceSensorListener, lightSensor, SENSOR_SAMPLE_US, SENSOR_BATCH_US);
            if (proximitySensor != null) sensorManager.registerListener(
                deviceSensorListener, proximitySensor, SENSOR_SAMPLE_US, SENSOR_BATCH_US);
            deviceSensorsRegistered = true;
            sensorClassifier.updateScreen(true, System.currentTimeMillis());
            Log.d(TAG, "设备传感器已注册（亮屏）");
        } catch (Exception e) {
            Log.w(TAG, "设备传感器注册失败: " + e.getMessage());
        }
    }

    private void onScreenOff() {
        sensorClassifier.updateScreen(false, System.currentTimeMillis());
        // 熄屏 5s 后注销（等传感器结算一次），省电
        mainHandler.postDelayed(this::stopContinuousMotion, 5_000L);
    }

    private void stopContinuousMotion() {
        if (sensorManager == null || !deviceSensorsRegistered) return;
        try {
            sensorManager.unregisterListener(deviceSensorListener);
        } catch (Exception ignored) {}
        deviceSensorsRegistered = false;
        Log.d(TAG, "设备传感器已注销（熄屏）");
    }

    private final SensorEventListener deviceSensorListener = new SensorEventListener() {
        @Override
        public void onSensorChanged(SensorEvent event) {
            long now = System.currentTimeMillis();
            int type = event.sensor.getType();
            if (type == Sensor.TYPE_GRAVITY && event.values.length >= 3) {
                sensorClassifier.updateGravity(
                    event.values[0], event.values[1], event.values[2], now);
            } else if (type == Sensor.TYPE_LINEAR_ACCELERATION && event.values.length >= 3) {
                sensorClassifier.updateMotion(
                    magnitude(event.values[0], event.values[1], event.values[2]), now);
            } else if (type == Sensor.TYPE_LIGHT) {
                sensorClassifier.updateLight(event.values[0], now);
            } else if (type == Sensor.TYPE_PROXIMITY) {
                sensorClassifier.updateProximity(
                    event.values[0], event.sensor.getMaximumRange(), now);
            }
        }

        @Override
        public void onAccuracyChanged(Sensor sensor, int accuracy) {}
    };

    private static float magnitude(float x, float y, float z) {
        return (float) Math.sqrt(x * x + y * y + z * z);
    }

    private final SensorEventListener stepListener = new SensorEventListener() {
        @Override
        public void onSensorChanged(SensorEvent event) {
            float current = event.values[0];
            latestStepCounter = current;
            String today = getLogicalDate();

            // 跨日重置
            if (!today.equals(stepLogicalDate) || stepDayStart < 0) {
                stepDayStart = current;
                stepLogicalDate = today;
                stepRebootOffset = 0;
                prefs.edit()
                    .putFloat("step_day_start_counter", stepDayStart)
                    .putString("step_logical_date", stepLogicalDate)
                    .putFloat("step_reboot_offset", stepRebootOffset)
                    .apply();
                return;
            }

            // 重启检测：传感器计数器回退 → 手机重启，需要救援已走步数
            // stepDayStart==0 意味着上次重启已清零基线，不应再次用 stepLastKnown-0
            // 做救援（否则手环终身累计值会被灌入 stepRebootOffset，造成步数膨胀）
            if (stepLastKnown >= 0 && current < stepLastKnown) {
                if (stepDayStart > 0) {
                    float rescued = stepLastKnown - stepDayStart;
                    stepRebootOffset += rescued;
                }
                // stepDayStart<=0 → rescue 已在上次重启时计入，不再重复加
                stepDayStart = 0;
                prefs.edit()
                    .putFloat("step_day_start_counter", 0f)
                    .putFloat("step_reboot_offset", stepRebootOffset)
                    .apply();
            }

            stepLastKnown = current;
            prefs.edit().putFloat("step_last_known_counter", current).apply();
        }

        @Override
        public void onAccuracyChanged(Sensor sensor, int accuracy) {}
    };

    private String getLogicalDate() {
        Calendar cal = Calendar.getInstance();
        if (cal.get(Calendar.HOUR_OF_DAY) < 5) cal.add(Calendar.DAY_OF_MONTH, -1);
        return new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.getTime());
    }

    private int getTodaySteps() {
        if (stepDayStart < 0) return 0;
        float current = latestStepCounter > 0 ? latestStepCounter : stepLastKnown;
        if (current < 0) return 0;
        return Math.max((int)(current - stepDayStart + stepRebootOffset), 0);
    }

    // 原生 BLE 心率读取（后台常驻，app 切后台也能采）
    private static final String HR_MAC = "";  // 由后端从 SQLite env 下发，默认为空（未配置）
    private static final String HR_SVC = "0000180d-0000-1000-8000-00805f9b34fb";
    private static final String HR_CHR = "00002a37-0000-1000-8000-00805f9b34fb";
    private static final String HR_CCCD = "00002902-0000-1000-8000-00805f9b34fb";

    private JSONObject readHeartRateBle(String mac) {
        JSONObject out = new JSONObject();
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                if (checkSelfPermission(android.Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
                    out.put("_success", false); out.put("_error", "[BLE:权限✗] 蓝牙连接权限未授予"); return out;
                }
            }
            BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
            BluetoothAdapter adapter = bm != null ? bm.getAdapter() : null;
            if (adapter == null || !adapter.isEnabled()) {
                out.put("_success", false); out.put("_error", "[BLE:蓝牙✗] 蓝牙未开启"); return out;
            }
            if (mac == null || mac.isEmpty()) {
                out.put("_success", false); out.put("_error", "[BLE] 未配置心率设备"); return out;
            }
            final int[] hrVal = {0};
            final String[] hrErr = {null};
            final CountDownLatch latch = new CountDownLatch(1);
            final BluetoothGatt[] gattHolder = {null};
            BluetoothDevice device = adapter.getRemoteDevice(mac);
            gattHolder[0] = device.connectGatt(this, false, new BluetoothGattCallback() {
                @Override public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
                    if (newState == BluetoothProfile.STATE_CONNECTED) g.discoverServices();
                    else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                        if (hrVal[0] == 0 && hrErr[0] == null)
                            hrErr[0] = "[BLE:GATT✗] 连接断开 status=" + status + (status == 133 ? "(设备不在范围)" : "");
                        latch.countDown();
                    }
                }
                @Override public void onServicesDiscovered(BluetoothGatt g, int status) {
                    BluetoothGattService svc = g.getService(java.util.UUID.fromString(HR_SVC));
                    if (svc == null) { hrErr[0] = "[BLE:服务✗] 无心率服务"; latch.countDown(); return; }
                    BluetoothGattCharacteristic ch = svc.getCharacteristic(java.util.UUID.fromString(HR_CHR));
                    if (ch == null) { hrErr[0] = "[BLE:特征✗] 无0x2A37"; latch.countDown(); return; }
                    g.setCharacteristicNotification(ch, true);
                    BluetoothGattDescriptor cccd = ch.getDescriptor(java.util.UUID.fromString(HR_CCCD));
                    if (cccd != null) {
                        cccd.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                        g.writeDescriptor(cccd);
                    } else g.readCharacteristic(ch);
                }
                @Override public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic ch) {
                    byte[] v = ch.getValue();
                    if (v != null && v.length >= 2)
                        hrVal[0] = (v[0] & 0x01) != 0 ? ((v[1] & 0xFF) | ((v[2] & 0xFF) << 8)) : (v[1] & 0xFF);
                    g.disconnect();
                }
                @Override public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic ch, int status) {
                    byte[] v = ch.getValue();
                    if (status == 0 && v != null && v.length >= 2)
                        hrVal[0] = (v[0] & 0x01) != 0 ? ((v[1] & 0xFF) | ((v[2] & 0xFF) << 8)) : (v[1] & 0xFF);
                    else if (hrErr[0] == null) hrErr[0] = "[BLE:读取✗] status=" + status;
                    g.disconnect();
                }
            });
            latch.await(10, TimeUnit.SECONDS);
            try { if (gattHolder[0] != null) gattHolder[0].close(); } catch (Exception ignored) {}
            if (hrVal[0] > 0) {
                out.put("_success", true); out.put("heart_rate", hrVal[0]); out.put("method", "ble_native");
            } else {
                out.put("_success", false); out.put("_error", hrErr[0] != null ? hrErr[0] : "[BLE:超时] 心率读取超时(10s)");
            }
        } catch (Exception e) {
            try { out.put("_success", false); out.put("_error", "[BLE:异常] " + e.getMessage()); } catch (Exception ignored) {}
        }
        return out;
    }

    // 列出已配对蓝牙设备（getBondedDevices → identity address，规避随机地址；仅需 BLUETOOTH_CONNECT）
    private JSONObject listBondedBleDevices() {
        JSONObject out = new JSONObject();
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                if (checkSelfPermission(android.Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
                    out.put("_success", false); out.put("_error", "[BLE:权限✗] 蓝牙连接权限未授予"); return out;
                }
            }
            BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
            BluetoothAdapter adapter = bm != null ? bm.getAdapter() : null;
            if (adapter == null || !adapter.isEnabled()) {
                out.put("_success", false); out.put("_error", "[BLE:蓝牙✗] 蓝牙未开启"); return out;
            }
            org.json.JSONArray arr = new org.json.JSONArray();
            for (BluetoothDevice d : adapter.getBondedDevices()) {
                JSONObject o = new JSONObject();
                o.put("address", d.getAddress());
                o.put("name", d.getName() != null ? d.getName() : "");
                o.put("type", d.getType());
                o.put("source", "bonded");
                arr.put(o);
            }
            out.put("_success", true);
            out.put("devices", arr);
        } catch (Exception e) {
            try { out.put("_success", false); out.put("_error", "[BLE:异常] " + e.getMessage()); } catch (Exception ignored) {}
        }
        return out;
    }

    private JSONObject getStepsData() {
        JSONObject data = new JSONObject();
        try {
            data.put("steps", (int) latestStepCounter);        // 传感器累计值（供 debug/兜底）
            data.put("counter", (int) latestStepCounter);
            data.put("today_steps", getTodaySteps());          // 权威今日步数（后端优先信任）
            // 有效性：day_start 已初始化 且 逻辑日一致 → today_steps 可信
            boolean valid = stepDayStart >= 0 && getLogicalDate().equals(stepLogicalDate);
            data.put("today_steps_valid", valid);
            data.put("logical_date", getLogicalDate());        // 手机 5点跨天逻辑日
            data.put("available", stepSensor != null && latestStepCounter >= 0);
        } catch (Exception ignored) {}
        return data;
    }

    // ================================================================
    // 数据采集 — GPS
    // ================================================================

    private void initLocation() {
        locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        locationListener = new LocationListener() {
            @Override public void onLocationChanged(Location loc) {
                lastLat = loc.getLatitude();
                lastLng = loc.getLongitude();
                lastGpsTime = System.currentTimeMillis();
                Log.d(TAG, "GPS: " + lastLat + "," + lastLng);
            }
            @Override public void onProviderDisabled(String provider) {}
            @Override public void onProviderEnabled(String provider) {}
            @Override public void onStatusChanged(String provider, int status, Bundle extras) {}
        };
    }

    private void requestGpsUpdate() {
        if (locationManager != null) {
            try {
                // 持续监听拿到新鲜定位，然后立即取消（避免耗电）
                locationManager.requestLocationUpdates(
                    LocationManager.NETWORK_PROVIDER, 0, 0, locationListener, mainHandler.getLooper());
                locationManager.requestLocationUpdates(
                    LocationManager.GPS_PROVIDER, 0, 0, locationListener, mainHandler.getLooper());
                // 10 秒后取消持续监听（拿到一次就够了）
                mainHandler.postDelayed(() -> {
                    try { locationManager.removeUpdates(locationListener); }
                    catch (Exception ignored) {}
                }, 10000);
            } catch (SecurityException ignored) {}
        }
    }

    private void startGpsThread() {
        // 启动时立即请求一次定位，不等 10 分钟
        requestGpsUpdate();
        gpsThread = new Thread(() -> {
            while (shouldRun) {
                try {
                    Thread.sleep(GPS_INTERVAL);
                    if (!shouldRun) break;
                    requestGpsUpdate();
                } catch (InterruptedException e) {
                    break;
                }
            }
        }, "MirrowGPS");
        gpsThread.setDaemon(true);
        gpsThread.start();
    }

    private void stopGpsThread() {
        if (gpsThread != null) gpsThread.interrupt();
        if (locationManager != null && locationListener != null) {
            try { locationManager.removeUpdates(locationListener); } catch (Exception ignored) {}
        }
    }

    // ================================================================
    // 数据采集 — 屏幕使用
    // ================================================================

    private void startUsageThread() {
        usageThread = new Thread(() -> {
            while (shouldRun) {
                try {
                    Thread.sleep(USAGE_INTERVAL);
                    if (!shouldRun) break;
                    // Usage 数据通过 mobile_usage 请求被动采集
                } catch (InterruptedException e) {
                    break;
                }
            }
        }, "MirrowUsage");
        usageThread.setDaemon(true);
        usageThread.start();
    }

    private void stopUsageThread() {
        if (usageThread != null) usageThread.interrupt();
    }

    // ================================================================
    // 无障碍周期自愈（2026-08-22 移植 AionsHome AccessibilityRecoveryController）
    // ================================================================

    /** ⚡ 每 5min 轻检查无障碍：isRunning()==true 直接跳过（零成本）；
     *  false（ColorOS 运行中自动关 / 进程重启后 Crashed 不重绑）→ ensureEnabled 恢复。
     *  敏感包（微信/支付宝/设置）前台或锁屏时跳过本轮，防写回再次被系统关（AionsHome 同款护栏）。 */
    private void startSelfHealThread() {
        selfHealThread = new Thread(() -> {
            while (shouldRun) {
                try {
                    Thread.sleep(SELF_HEAL_INTERVAL_MS);
                    if (!shouldRun) break;
                    if (!isSafeToSelfHeal()) continue;
                    if (!MirrowAccessibilityService.isRunning()) {
                        String r = MirrowAccessibilityService.ensureEnabled(MirrowRelayService.this);
                        MainApplication.log("A11Y", "periodic ensureEnabled: " + r, null);
                    }
                } catch (InterruptedException e) {
                    break;
                } catch (Throwable t) {
                    MainApplication.log("A11Y", "periodic ensureEnabled crash: " + t.getMessage(), t);
                }
            }
        }, "MirrowSelfHeal");
        selfHealThread.setDaemon(true);
        selfHealThread.start();
    }

    private boolean isSafeToSelfHeal() {
        try {
            if (SENSITIVE_SELF_HEAL_PACKAGES.contains(currentAppPackage)) return false;  // 敏感包前台
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            return pm == null || pm.isInteractive();  // 锁屏跳过
        } catch (Throwable t) {
            return true;  // 异常放行，宁可自愈一次
        }
    }

    private JSONObject getUsageData() {
        JSONObject data = new JSONObject();
        try {
            UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
            if (usm == null) {
                data.put("available", false);
                data.put("error", "usage_stats_manager_unavailable");
                return data;
            }
            AppOpsManager appOps = (AppOpsManager) getSystemService(Context.APP_OPS_SERVICE);
            if (appOps != null && appOps.checkOpNoThrow(
                    AppOpsManager.OPSTR_GET_USAGE_STATS,
                    android.os.Process.myUid(), getPackageName()) != AppOpsManager.MODE_ALLOWED) {
                data.put("available", false);
                data.put("error", "usage_access_not_granted");
                return data;
            }
            long now = System.currentTimeMillis();
            long dayStart = PerceptionClock.logicalDayStartMillis();
            Map<String, Long> foregroundMap = computeUsageForegroundTime(usm, dayStart, now);
            boolean fromEvents = !foregroundMap.isEmpty();
            if (!fromEvents) {
                List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_BEST, dayStart, now);
                long maxPossibleMs = Math.max(0L, now - dayStart);
                if (stats != null) {
                    for (UsageStats s : stats) {
                        long capped = Math.min(Math.max(0L, s.getTotalTimeInForeground()), maxPossibleMs);
                        if (capped > 0) foregroundMap.put(s.getPackageName(), capped);
                    }
                }
            }

            long observedTotal = 0L;
            for (Long duration : foregroundMap.values()) observedTotal += Math.max(0L, duration);
            long maxPossibleMs = Math.max(0L, now - dayStart);
            if (!fromEvents && observedTotal > maxPossibleMs && observedTotal > 0L) {
                double scale = (double) maxPossibleMs / (double) observedTotal;
                for (Map.Entry<String, Long> entry : foregroundMap.entrySet()) {
                    entry.setValue((long) (Math.max(0L, entry.getValue()) * scale));
                }
            }

            List<Map.Entry<String, Long>> sorted = new ArrayList<>(foregroundMap.entrySet());
            Collections.sort(sorted, (a, b) -> Long.compare(b.getValue(), a.getValue()));
            org.json.JSONArray topApps = new org.json.JSONArray();
            long totalMs = 0L;
            int count = 0;
            for (Map.Entry<String, Long> entry : sorted) {
                long duration = Math.max(0L, entry.getValue());
                totalMs += duration;
                long minutes = duration / 60000L;
                if (minutes > 0 && count < 15) {
                    JSONObject app = new JSONObject();
                    app.put("package", entry.getKey());
                    app.put("minutes", minutes);
                    topApps.put(app);
                    count++;
                }
            }
            data.put("total_minutes", totalMs / 60000);
            data.put("top_apps", topApps);
            data.put("app_count", count);
            data.put("source", fromEvents ? "usage_events" : "fallback_usage_stats");
            data.put("logical_date", PerceptionClock.logicalDate());
            data.put("window_start_ms", dayStart);
            data.put("available", true);
        } catch (Exception e) {
            try {
                data.put("available", false);
                data.put("error", e.getMessage() != null ? e.getMessage() : "usage_read_failed");
            } catch (Exception ignored) {}
        }
        return data;
    }

    private Map<String, Long> computeUsageForegroundTime(
            UsageStatsManager usm, long start, long end) {
        Map<String, Long> durations = new LinkedHashMap<>();
        try {
            UsageEvents events = usm.queryEvents(start, end);
            UsageEvents.Event event = new UsageEvents.Event();
            String currentPackage = null;
            long currentStart = 0L;
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                String pkg = event.getPackageName();
                if (pkg == null) continue;
                int type = event.getEventType();
                if (type == UsageEvents.Event.ACTIVITY_RESUMED
                        || type == UsageEvents.Event.MOVE_TO_FOREGROUND) {
                    if (currentPackage != null && currentStart > 0L) {
                        addUsageDuration(durations, currentPackage, event.getTimeStamp() - currentStart);
                    }
                    currentPackage = pkg;
                    currentStart = event.getTimeStamp();
                } else if (type == UsageEvents.Event.ACTIVITY_PAUSED
                        || type == UsageEvents.Event.ACTIVITY_STOPPED
                        || type == UsageEvents.Event.MOVE_TO_BACKGROUND) {
                    if (currentPackage != null && currentPackage.equals(pkg) && currentStart > 0L) {
                        addUsageDuration(durations, currentPackage, event.getTimeStamp() - currentStart);
                        currentPackage = null;
                        currentStart = 0L;
                    }
                }
            }
            if (currentPackage != null && currentStart > 0L) {
                addUsageDuration(durations, currentPackage, end - currentStart);
            }
        } catch (Exception e) {
            Log.w(TAG, "usage events calculation failed: " + e.getMessage());
        }
        return durations;
    }

    private void addUsageDuration(Map<String, Long> durations, String pkg, long duration) {
        if (duration <= 0L) return;
        Long existing = durations.get(pkg);
        durations.put(pkg, (existing != null ? existing : 0L) + duration);
    }

    // ================================================================
    // 数据采集 — 电池
    // ================================================================

    private JSONObject getBatteryData() {
        JSONObject data = new JSONObject();
        try {
            IntentFilter ifilter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
            Intent batteryStatus = registerReceiver(null, ifilter);
            if (batteryStatus != null) {
                int level = batteryStatus.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = batteryStatus.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                int pct = scale > 0 ? level * 100 / scale : -1;
                data.put("level", pct);
                data.put("charging", batteryStatus.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0);
                data.put("available", true);
            }
        } catch (Exception ignored) {}
        return data;
    }

    // ================================================================
    // 屏幕开关广播
    // ================================================================

    private void initScreenReceiver() {
        screenReceiver = new ScreenReceiver();
        IntentFilter filter = new IntentFilter();
        filter.addAction(Intent.ACTION_SCREEN_ON);
        filter.addAction(Intent.ACTION_SCREEN_OFF);
        registerReceiver(screenReceiver, filter);
    }

    /** Reconcile broadcasts with the live interactive flag (OPPO AOD can omit a broadcast). */
    private void reconcileScreenState() {
        boolean interactive = readScreenInteractive();
        if (interactive == screenOn) return;
        String previousForeground = currentAppPackage == null ? "" : currentAppPackage;
        screenOn = interactive;
        if (interactive) {
            recordRelayDiagnostic("screen_on", "previous_foreground=" + previousForeground);
            detectForegroundApp();
            startForScreenOn();
        } else {
            // The accessibility window cache is usually fresher than the last
            // 60s sampler. Capture it before clearing the foreground state so
            // the sleep boundary names the app actually in use.
            detectForegroundApp();
            if (currentAppPackage != null && !currentAppPackage.isEmpty()) {
                previousForeground = currentAppPackage;
            }
            recordRelayDiagnostic("screen_off", "last_foreground=" + previousForeground);
            currentAppPackage = "";
            currentAppName = "";
            currentAppResolution = "screen_off";
            currentAppObservedAtMs = 0L;
            MirrowAccessibilityService.clearLastWindowState();
            onScreenOff();
        }
    }

    private void unregisterScreenReceiver() {
        if (screenReceiver != null) {
            try { unregisterReceiver(screenReceiver); } catch (Exception ignored) {}
        }
    }

    class ScreenReceiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (intent == null) return;
            if (Intent.ACTION_SCREEN_ON.equals(intent.getAction())
                    || Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) {
                // Use PowerManager's live value rather than trusting the
                // broadcast action when ColorOS/AOD emits them late.
                reconcileScreenState();
                com.mirrow.app.widget.WanderWidget.wake();
            }
        }
    }

    // ================================================================
    // 网络回调
    // ================================================================

    private void initNetworkCallback() {
        ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return;
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                // 1. 触发 WebSocket 重连
                if (!wsConnected.get() && shouldRun) {
                    reconnectDelay = BASE_RECONNECT_DELAY;
                    scheduleReconnect("network_available", 0);
                }
                RelayWatchdogController.triggerRepair(MirrowRelayService.this, "network_available");
                // 2. 检测 WiFi 连接 → 推送事件
                try {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(network);
                    if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                        mainHandler.post(() -> {
                            JSONObject info = getCurrentWifiInfo();
                            if (info.optBoolean("connected", false)) {
                                String ssid = info.optString("ssid", "");
                                String bssid = info.optString("bssid", "");
                                onWifiConnected(ssid, bssid);
                            }
                        });
                    }
                } catch (Exception e) {
                    Log.w(TAG, "WiFi 检测失败: " + e.getMessage());
                }
            }

            @Override
            public void onCapabilitiesChanged(Network network, NetworkCapabilities caps) {
                if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                    reconcileWifiState();
                }
            }
            @Override
            public void onLost(Network network) {
                wsConnected.set(false);
                lastDisconnectTime = System.currentTimeMillis();
                RelayWatchdogController.onDisconnected(MirrowRelayService.this, "network_lost");
                scheduleReconnect("network_lost", BASE_RECONNECT_DELAY);
                updateKeepAlive("网络断开，等待恢复...");
                // 检测 WiFi 是否也断了
                try {
                    ConnectivityManager cm2 = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
                    if (cm2 != null) {
                        Network current = cm2.getActiveNetwork();
                        if (current == null) {
                            mainHandler.post(() -> onWifiDisconnected());
                        } else {
                            NetworkCapabilities caps = cm2.getNetworkCapabilities(current);
                            if (caps == null || !caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                                mainHandler.post(() -> onWifiDisconnected());
                            }
                        }
                    }
                } catch (Exception e) {
                    Log.w(TAG, "WiFi 断开检测失败: " + e.getMessage());
                }
            }
        };
        NetworkRequest req = new NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build();
        cm.registerNetworkCallback(req, networkCallback);
    }

    private void unregisterNetworkCallback() {
        if (networkCallback != null) {
            try {
                ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
                if (cm != null) cm.unregisterNetworkCallback(networkCallback);
            } catch (Exception ignored) {}
        }
    }

    // ================================================================
    // 辅助
    // ================================================================

    private long getDayStartMillis() {
        return PerceptionClock.logicalDayStartMillis();
    }

    private Map<String, String> getAppNameMap() {
        Map<String, String> map = new HashMap<>();
        map.put("com.tencent.mm", "微信");
        map.put("com.tencent.mobileqq", "QQ");
        map.put("com.xingin.xhs", "小红书");
        map.put("com.sina.weibo", "微博");
        map.put("com.ss.android.ugc.aweme", "抖音");
        map.put("tv.danmaku.bili", "哔哩哔哩");
        map.put("com.tencent.qqlive", "腾讯视频");
        map.put("com.qiyi.video", "爱奇艺");
        map.put("com.taobao.taobao", "淘宝");
        map.put("com.jingdong.app.mall", "京东");
        map.put("com.netease.cloudmusic", "网易云音乐");
        map.put("com.netease.uuremote", "UU远程");
        map.put("com.miHoYo.Yuanshen", "原神");
        map.put("com.miHoYo.hkrpg", "崩坏：星穹铁道");
        map.put("com.tencent.tmgp.sgame", "王者荣耀");
        map.put("com.mirrow.app", "MIRROW");
        map.put("com.autonavi.minimap", "高德地图");
        map.put("com.sankuai.meituan", "美团");
        map.put("com.zhihu.android", "知乎");
        map.put("com.android.systemui", null);
        map.put("com.android.launcher", null);
        map.put("com.android.launcher3", null);
        map.put("com.miui.home", null);
        return map;
    }
}
