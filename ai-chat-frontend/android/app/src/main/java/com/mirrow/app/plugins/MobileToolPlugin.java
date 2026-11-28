package com.mirrow.app.plugins;

import android.app.usage.UsageEvents;
import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.content.ContentValues;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Environment;
import android.os.PowerManager;
import android.os.ParcelUuid;
import android.provider.Settings;
import android.provider.MediaStore;
import android.net.Uri;
import android.util.Base64;
import android.widget.Toast;
import android.util.Log;

import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageCapture;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.camera.core.ImageCaptureException;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.core.content.ContextCompat;
import androidx.lifecycle.LifecycleOwner;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.mirrow.app.FloatingWidgetService;
import com.mirrow.app.BuildConfig;
import com.mirrow.app.MainApplication;
import com.mirrow.app.MirrowAccessibilityService;
import com.mirrow.app.MirrowRelayService;
import com.mirrow.app.PerceptionClock;
import com.mirrow.app.network.BackendEndpointPool;

import com.google.common.util.concurrent.ListenableFuture;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.OutputStream;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;

/**
 * 手机端工具执行插件。截图使用无障碍服务，设备信息使用 Android 系统接口。
 */
@CapacitorPlugin(name = "MobileTool")
public class MobileToolPlugin extends Plugin {
    private static final String TAG = "MIRROW_MobileTool";

    // ── SharedPreferences 存储 key ──
    private static final String PREFS_NAME = BackendEndpointPool.PREFS_NAME;
    private static final String KEY_BACKEND_URL = "backend_url";
    private static final String DEFAULT_BACKEND_URL = BuildConfig.MIRROW_BACKEND_FALLBACK_URL;

    // ⚡ 截图去重缓存——防止无障碍 takeScreenshot 返回 stale framebuffer
    private String _lastScreenshotHash = "";

    // ── 甜焰 BLE 单例（与原生后台 Relay 共享） ──
    private BLEController getToy2Ble() {
        return BLEController.getInstance(getContext());
    }

    @Override
    public void load() {
        Log.i(TAG, "MobileTool 插件已加载");
        widgetPanel(getActivity().getIntent());
    }

    @Override protected void handleOnNewIntent(Intent intent) {
        widgetPanel(intent);
    }

    private void widgetPanel(Intent intent) {
        if (intent == null) return;
        String panel = intent.getStringExtra("wander_widget_panel");
        if (!"mood".equals(panel) && !"activity".equals(panel)) return;
        intent.removeExtra("wander_widget_panel");
        JSObject event = new JSObject();
        event.put("panel", panel);
        notifyListeners("wanderWidgetPanel", event, true);
    }

    @PluginMethod
    public void setWidgetAvatar(PluginCall call) {
        String png = call.getString("png", "");
        if (png.isEmpty() || png.length() > 160000) {
            call.reject("Invalid thumbnail");
            return;
        }
        android.util.AtomicFile file = new android.util.AtomicFile(com.mirrow.app.widget.WanderWidget.avatarFile(getContext()));
        java.io.FileOutputStream out = null;
        try {
            byte[] bytes = android.util.Base64.decode(png, android.util.Base64.DEFAULT);
            android.graphics.BitmapFactory.Options bounds = new android.graphics.BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.length, bounds);
            if (bounds.outWidth != 128 || bounds.outHeight != 128 || !"image/png".equals(bounds.outMimeType)) {
                call.reject("Invalid thumbnail");
                return;
            }
            out = file.startWrite();
            out.write(bytes);
            file.finishWrite(out);
        } catch (Exception e) {
            if (out != null) file.failWrite(out);
            call.reject("Unable to save thumbnail");
            return;
        }
        new android.os.Handler(android.os.Looper.getMainLooper()).post(
                () -> com.mirrow.app.widget.WanderWidget.render(getContext()));
        call.resolve();
    }

    @PluginMethod
    public void execute(PluginCall call) {
        String tool = call.getString("tool");
        JSObject result = new JSObject();

        Log.i(TAG, "execute tool=" + tool);
        MainApplication.log("TOOL", "execute: " + tool, null);

        try {
            switch (tool) {
                case "mobile_miband": {
                    new Thread(() -> {
                        try {
                            call.resolve(JSObject.fromJSONObject(com.mirrow.app.miband.MiBandBridge.execute(getContext(), new org.json.JSONObject(call.getString("params", "{}")))));
                        } catch (Exception e) { call.reject("手环操作失败：" + e.getMessage()); }
                    }, "mirrow-band-relay").start();
                    return;
                }
                // ── 无障碍截图服务状态（查 + 自动修复）──
                case "accessibility_status": {
                    boolean a11yRunning = com.mirrow.app.MirrowAccessibilityService.isRunning();
                    String selfHeal = "";
                    if (!a11yRunning) {
                        selfHeal = com.mirrow.app.MirrowAccessibilityService.ensureEnabled(getContext());
                    }
                    boolean secureGranted = getContext().checkSelfPermission(
                            android.Manifest.permission.WRITE_SECURE_SETTINGS)
                            == android.content.pm.PackageManager.PERMISSION_GRANTED;
                    String content;
                    if (a11yRunning) {
                        content = "无障碍截图服务已开启";
                    } else if (selfHeal.startsWith("healed")) {
                        content = "无障碍服务已自动修复（系统重新绑定中，几秒后再点检测确认）";
                    } else {
                        content = "无障碍截图服务未开启"
                            + (secureGranted ? "" : "（自愈未授权，需一次性 adb pm grant）");
                    }
                    result.put("content", content);
                    result.put("running", a11yRunning);
                    result.put("self_heal", selfHeal);
                    result.put("write_secure_granted", secureGranted);
                    result.put("success", true);
                    MainApplication.log("TOOL", "accessibility_status: running=" + a11yRunning
                        + " self_heal=" + selfHeal + " granted=" + secureGranted, null);
                    break;
                }

                // ── 打开无障碍设置页（供用户一次性开启 MIRROW 屏幕感知）──
                case "open_accessibility_settings": {
                    try {
                        Intent intent = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
                        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        getContext().startActivity(intent);
                        result.put("content", "已打开无障碍设置");
                        result.put("success", true);
                    } catch (Exception e) {
                        result.put("content", "打开无障碍设置失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 屏幕活跃度 ──
                case "mobile_active_duration": {
                    try {
                        // PowerManager.isInteractive() — 系统原生接口
                        PowerManager pm = (PowerManager) getContext().getSystemService(Context.POWER_SERVICE);
                        boolean screenOn = pm != null && pm.isInteractive();

                        long sessionDurationMs = 0;
                        long totalScreenMsToday = 0;

                        // 方案二：UsageEvents SCREEN_INTERACTIVE 事件算时长（抄 AionsHome 思路）
                        UsageStatsManager usm = (UsageStatsManager) getContext()
                                .getSystemService(Context.USAGE_STATS_SERVICE);
                        if (usm != null) {
                            long now = System.currentTimeMillis();
                            long dayStart = now - (now % 86400000L);
                            UsageEvents events = usm.queryEvents(dayStart - 3600_000L, now);
                            UsageEvents.Event event = new UsageEvents.Event();
                            long lastScreenInteractive = 0;

                            while (events.hasNextEvent()) {
                                events.getNextEvent(event);
                                if (event.getEventType() == UsageEvents.Event.SCREEN_INTERACTIVE) {
                                    lastScreenInteractive = event.getTimeStamp();
                                    // 累计今天的亮屏总时长
                                    // 找对应的 SCREEN_NON_INTERACTIVE
                                } else if (event.getEventType() == UsageEvents.Event.SCREEN_NON_INTERACTIVE) {
                                    totalScreenMsToday += event.getTimeStamp() - lastScreenInteractive;
                                    lastScreenInteractive = 0;
                                }
                            }
                            // 如果屏幕当前亮着，加上从最后一次亮屏到现在的时长
                            if (lastScreenInteractive > 0 && screenOn) {
                                totalScreenMsToday += now - lastScreenInteractive;
                                sessionDurationMs = now - lastScreenInteractive;
                            }
                        }

                        long sessionMin = sessionDurationMs / 60000;
                        long totalMin = totalScreenMsToday / 60000;

                        StringBuilder sb = new StringBuilder();
                        sb.append("[活跃] 屏幕").append(screenOn ? "亮着" : "已熄");
                        if (screenOn && sessionMin > 0) {
                            sb.append(" | 本次亮屏约 ").append(sessionMin).append(" 分钟");
                        }
                        if (totalMin > 0) {
                            sb.append(" | 今日累计屏幕使用约 ").append(totalMin).append(" 分钟");
                        }

                        result.put("content", sb.toString());
                        result.put("screen_on", screenOn);
                        result.put("session_duration_minutes", (int) sessionMin);
                        result.put("total_screen_minutes_today", (int) totalMin);
                        result.put("success", true);
                    } catch (Exception e) {
                        Log.w(TAG, "mobile_active_duration error: " + e.getMessage());
                        result.put("content", "屏幕活跃状态暂时无法读取");
                        result.put("success", false);
                    }
                    break;
                }

                // ── 通知 ──
                case "mobile_notifications": {
                    // ⚡ 2026-08-22：改为从 NotificationListener 缓存读（用户已授权"通知访问"），
                    // 2026-08-24：透传 category/ongoing/count（噪声已在缓存层过滤）
                    String[][] notifs = com.mirrow.app.MirrowNotificationListener.getRecentNotifications();
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
                    Log.i(TAG, "mobile_notifications: " + arr.length() + " 条 (NotificationListener)");
                    break;
                }

                // ── 设备姿态/运动/光线（2026-08-24 AionsHome 移植）──
                case "mobile_sensor_context": {
                    // 前台路径读后台服务共享的单例缓存（MirrowRelayService 常驻采集更准）
                    org.json.JSONObject snap = com.mirrow.app.DeviceContextClassifier
                        .getInstance().snapshot(System.currentTimeMillis());
                    result.put("data", snap);
                    result.put("success", true);
                    Log.i(TAG, "mobile_sensor_context: " + snap.length() + " slots (前台)");
                    break;
                }

                // ── 截屏 ──
                case "mobile_screenshot": {
                    final long ssT0 = System.currentTimeMillis();
                    Log.i(TAG, "[JAVA:SS] ═══ entry ═══");
                    String img = null;
                    long ssDt = 0;
                    String method = "";

                    // 无障碍截图（API30+）
                    if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R
                            && com.mirrow.app.MirrowAccessibilityService.isRunning()) {
                        try {
                            String a11yImg = com.mirrow.app.MirrowAccessibilityService.getInstance()
                                .captureScreenshotBase64(1080, 82, 4000);
                            if (a11yImg != null && a11yImg.length() > 100) {
                                // ⚡ 去重检测：内容感知 hash（前/中/尾多点采样，避开 JPEG 固定文件头——
                                //   旧实现取前 200 字符落固定头区，不同内容也判同 → 每次截图都被当"缓存"）
                                String currentHash = com.mirrow.app.MirrowAccessibilityService
                                    .screenshotContentHash(a11yImg);
                                if (currentHash.equals(_lastScreenshotHash)) {
                                    // 疑似缓存 → 重试一次（对齐后台路径），仍相同则报告截图不可用
                                    Log.w(TAG, "[JAVA:SS] L-1 a11y 疑似缓存截图 (hash=" + currentHash + ")，重试一次");
                                    Thread.sleep(150);
                                    String retry = com.mirrow.app.MirrowAccessibilityService.getInstance()
                                        .captureScreenshotBase64(1080, 82, 4000);
                                    if (retry != null && retry.length() > 100
                                            && !com.mirrow.app.MirrowAccessibilityService
                                                .screenshotContentHash(retry).equals(_lastScreenshotHash)) {
                                        a11yImg = retry;  // 重试拿到新帧
                                        currentHash = com.mirrow.app.MirrowAccessibilityService
                                            .screenshotContentHash(a11yImg);
                                    } else {
                                        a11yImg = null;  // 重试仍缓存/失败 → 报告截图不可用
                                    }
                                }
                                if (a11yImg != null && a11yImg.length() > 100) {
                                    img = a11yImg;
                                    method = "a11y";
                                    _lastScreenshotHash = currentHash;
                                    ssDt = System.currentTimeMillis() - ssT0;
                                    Log.i(TAG, "[JAVA:SS] L-1 a11y OK (" + a11yImg.length() + " chars, hash=" + currentHash + ") in " + ssDt + "ms");
                                }
                            }
                        } catch (Throwable t) {
                            Log.w(TAG, "[JAVA:SS] a11y failed: " + t.getMessage());
                        }
                    }

                    int imgLen = (img != null) ? img.length() : 0;
                    Log.i(TAG, "[JAVA:SS] done in " + ssDt + "ms, len=" + imgLen + " method=" + method);
                    String diag = method.isEmpty() ? " | all methods failed" : "";
                    if (imgLen == 0) {
                        // ⚡ 失败时附 a11y errorCode，便于后端/前端区分根因（interval_limit / no_access / error_N）
                        String a11yErr = com.mirrow.app.MirrowAccessibilityService.getLastShotError();
                        if (!a11yErr.isEmpty()) diag += " a11y=" + a11yErr;
                    }
                    result.put("content", "[SS:" + ssDt + "ms|" + (method.isEmpty() ? "FAIL" : method) + "] " + (imgLen > 0 ? "OK (" + imgLen + " chars)" : "FAILED" + diag));
                    result.put("image_base64", img);
                    result.put("success", imgLen > 0);
                    break;
                }

                // ── 摄像头拍照 ──
                case "mobile_camera": {
                    final long javaT0 = System.currentTimeMillis();
                    Log.i(TAG, "[JAVA:CAM] ═══ entry ═══");
                    MainApplication.log("TOOL", "[JAVA:CAM] entry", null);
                    // Parse facing from params JSON
                    int lensFacing = CameraSelector.LENS_FACING_BACK;
                    try {
                        String paramsStr = call.getString("params");
                        if (paramsStr != null && !paramsStr.isEmpty()) {
                            org.json.JSONObject paramsJson = new org.json.JSONObject(paramsStr);
                            String facing = paramsJson.optString("facing", "back");
                            if ("front".equals(facing)) {
                                lensFacing = CameraSelector.LENS_FACING_FRONT;
                            }
                        }
                    } catch (Exception ignored) {}

                    final android.app.Activity activity = getActivity();

                    // Check camera permission — request if not granted
                    if (ContextCompat.checkSelfPermission(getContext(), android.Manifest.permission.CAMERA)
                            != PackageManager.PERMISSION_GRANTED) {
                        // Request runtime permission (shows system dialog)
                        if (activity != null) {
                            androidx.core.app.ActivityCompat.requestPermissions(
                                    activity,
                                    new String[]{android.Manifest.permission.CAMERA},
                                    200);
                        }
                        long dt = System.currentTimeMillis() - javaT0;
                        result.put("content", "[JAVA:CAM:" + dt + "ms] 相机权限未授予，已弹出授权对话框。请允许后重试。");
                        result.put("success", false);
                        Log.w(TAG, "[JAVA:CAM] permission requested at " + dt + "ms");
                        break;
                    }

                    Log.i(TAG, "mobile_camera: capture start, facing=" + (lensFacing == CameraSelector.LENS_FACING_FRONT ? "front" : "back"));
                    MainApplication.log("TOOL", "mobile_camera capture start", null);

                    if (activity == null) {
                        result.put("content", "[拍照] 失败: Activity 不可用");
                        result.put("success", false);
                        break;
                    }

                    final int finalLensFacing = lensFacing;
                    final CountDownLatch latch = new CountDownLatch(1);
                    final byte[][] imageBytesHolder = new byte[1][];
                    final String[] errorHolder = new String[1];
                    final Executor mainExecutor = ContextCompat.getMainExecutor(getContext());

                    // CameraX must init on main thread
                    activity.runOnUiThread(() -> {
                        try {
                            com.google.common.util.concurrent.ListenableFuture<ProcessCameraProvider> cameraFuture =
                                    ProcessCameraProvider.getInstance(getContext());
                            cameraFuture.addListener(() -> {
                                try {
                                    ProcessCameraProvider provider = cameraFuture.get();

                                    ImageCapture imageCapture = new ImageCapture.Builder()
                                            .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                                            .setTargetResolution(new android.util.Size(1024, 1024))
                                            .build();

                                    CameraSelector selector = new CameraSelector.Builder()
                                            .requireLensFacing(finalLensFacing)
                                            .build();

                                    provider.unbindAll();
                                    provider.bindToLifecycle(
                                            (LifecycleOwner) activity,
                                            selector,
                                            imageCapture
                                    );

                                    // Capture to temp file (avoids manual YUV→JPEG conversion)
                                    java.io.File tempFile = new java.io.File(
                                            getContext().getCacheDir(), "mirrow_camera_" + System.currentTimeMillis() + ".jpg");
                                    ImageCapture.OutputFileOptions outputOptions =
                                            new ImageCapture.OutputFileOptions.Builder(tempFile).build();

                                    imageCapture.takePicture(
                                            outputOptions,
                                            mainExecutor,
                                            new ImageCapture.OnImageSavedCallback() {
                                                @Override
                                                public void onImageSaved(ImageCapture.OutputFileResults output) {
                                                    try {
                                                        // Read file bytes (compat with API 24)
                                                        byte[] fileBytes = new byte[(int) tempFile.length()];
                                                        java.io.FileInputStream fis = new java.io.FileInputStream(tempFile);
                                                        fis.read(fileBytes);
                                                        fis.close();
                                                        tempFile.delete();

                                                        // Decode and re-encode at quality 75 + max 1024px
                                                        Bitmap bitmap = BitmapFactory.decodeByteArray(fileBytes, 0, fileBytes.length);
                                                        if (bitmap != null) {
                                                            int w = bitmap.getWidth();
                                                            int h = bitmap.getHeight();
                                                            int maxDim = Math.max(w, h);
                                                            if (maxDim > 1024) {
                                                                float ratio = 1024f / maxDim;
                                                                bitmap = Bitmap.createScaledBitmap(bitmap,
                                                                        Math.round(w * ratio), Math.round(h * ratio), true);
                                                            }
                                                            ByteArrayOutputStream baos = new ByteArrayOutputStream();
                                                            bitmap.compress(Bitmap.CompressFormat.JPEG, 75, baos);
                                                            imageBytesHolder[0] = baos.toByteArray();
                                                            baos.close();
                                                        } else {
                                                            // BitmapFactory failed, use raw file bytes
                                                            imageBytesHolder[0] = fileBytes;
                                                        }

                                                        Log.i(TAG, "mobile_camera: JPEG size=" + imageBytesHolder[0].length + " bytes");
                                                    } catch (Exception e) {
                                                        errorHolder[0] = "File read error: " + e.getMessage();
                                                        Log.e(TAG, "mobile_camera: " + errorHolder[0], e);
                                                    } finally {
                                                        provider.unbindAll();
                                                        latch.countDown();
                                                    }
                                                }

                                                @Override
                                                public void onError(ImageCaptureException exception) {
                                                    errorHolder[0] = "Capture error: " + exception.getMessage();
                                                    Log.e(TAG, "mobile_camera: " + errorHolder[0], exception);
                                                    provider.unbindAll();
                                                    latch.countDown();
                                                }
                                            }
                                    );
                                } catch (Exception e) {
                                    errorHolder[0] = "Camera setup error: " + e.getMessage();
                                    Log.e(TAG, "mobile_camera: " + errorHolder[0], e);
                                    latch.countDown();
                                }
                            }, mainExecutor);
                        } catch (Exception e) {
                            errorHolder[0] = "CameraX init error: " + e.getMessage();
                            Log.e(TAG, "mobile_camera: " + errorHolder[0], e);
                            latch.countDown();
                        }
                    });

                    // Wait for capture (8s — fail fast if camera isn't available)
                    try {
                        boolean completed = latch.await(8, TimeUnit.SECONDS);
                        if (!completed) errorHolder[0] = "Camera timed out after 8s";
                    } catch (InterruptedException e) {
                        errorHolder[0] = "Camera interrupted";
                    }

                    long javaDt = System.currentTimeMillis() - javaT0;
                    if (imageBytesHolder[0] != null && imageBytesHolder[0].length > 0) {
                        String b64 = Base64.encodeToString(imageBytesHolder[0], Base64.NO_WRAP);
                        Log.i(TAG, "[JAVA:CAM] success at " + javaDt + "ms, base64=" + b64.length());
                        MainApplication.log("TOOL", "[CAM:" + javaDt + "ms] done " + b64.length() + " chars", null);
                        result.put("content", "[JAVA:CAM:" + javaDt + "ms] OK (" + b64.length() + " chars)");
                        result.put("image_base64", b64);
                        result.put("success", true);
                    } else {
                        String err = errorHolder[0] != null ? errorHolder[0] : "Unknown error";
                        Log.e(TAG, "[JAVA:CAM] failed at " + javaDt + "ms: " + err);
                        MainApplication.log("TOOL", "[CAM:" + javaDt + "ms] FAIL: " + err, null);
                        result.put("content", "[JAVA:CAM:" + javaDt + "ms] FAIL: " + err);
                        result.put("success", false);
                    }
                    break;
                }

                // ── 🔋 电池电量+充电状态 ──
                case "mobile_battery": {
                    try {
                        IntentFilter filter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
                        Intent batteryIntent = getContext().registerReceiver(null, filter);
                        if (batteryIntent != null) {
                            int level = batteryIntent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                            int scale = batteryIntent.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
                            int pct = (int) (level * 100.0 / scale);
                            int status = batteryIntent.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                            int plugged = batteryIntent.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1);
                            int temp = batteryIntent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);

                            String statusStr;
                            switch (status) {
                                case BatteryManager.BATTERY_STATUS_CHARGING: statusStr = "充电中"; break;
                                case BatteryManager.BATTERY_STATUS_DISCHARGING: statusStr = "放电中"; break;
                                case BatteryManager.BATTERY_STATUS_FULL: statusStr = "已充满"; break;
                                case BatteryManager.BATTERY_STATUS_NOT_CHARGING: statusStr = "未充电"; break;
                                default: statusStr = "未知";
                            }

                            String chargeType = "";
                            if (plugged == BatteryManager.BATTERY_PLUGGED_AC) chargeType = " (AC电源)";
                            else if (plugged == BatteryManager.BATTERY_PLUGGED_USB) chargeType = " (USB)";
                            else if (plugged == BatteryManager.BATTERY_PLUGGED_WIRELESS) chargeType = " (无线充电)";

                            org.json.JSONObject data = new org.json.JSONObject();
                            data.put("level", pct);
                            data.put("status", statusStr + chargeType);
                            data.put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL);
                            if (temp > 0) data.put("temperature", temp / 10.0);

                            result.put("data", data);
                            result.put("content", "电量 " + pct + "% | " + statusStr + chargeType + (temp > 0 ? " | " + (temp/10.0) + "°C" : ""));
                            result.put("success", true);
                        } else {
                            result.put("content", "无法读取电池信息");
                            result.put("success", false);
                        }
                    } catch (Exception e) {
                        result.put("content", "电池读取失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 📊 屏幕使用时间+App使用统计 ──
                case "mobile_usage": {
                    try {
                        UsageStatsManager usm = (UsageStatsManager) getContext().getSystemService(Context.USAGE_STATS_SERVICE);
                        if (usm == null) {
                            result.put("content", "UsageStatsManager 不可用");
                            result.put("success", false);
                            break;
                        }

                        // Check if usage access permission is granted
                        android.app.AppOpsManager appOps = (android.app.AppOpsManager) getContext().getSystemService(Context.APP_OPS_SERVICE);
                        if (appOps != null) {
                            int mode = appOps.checkOpNoThrow(android.app.AppOpsManager.OPSTR_GET_USAGE_STATS,
                                    android.os.Process.myUid(), getContext().getPackageName());
                            if (mode != android.app.AppOpsManager.MODE_ALLOWED) {
                                try {
                                    Intent intent = new Intent(android.provider.Settings.ACTION_USAGE_ACCESS_SETTINGS);
                                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                                    if (getActivity() != null) {
                                        getActivity().startActivity(intent);
                                    } else if (getContext() != null) {
                                        getContext().startActivity(intent);
                                    }
                                } catch (Exception e2) {
                                    Log.w(TAG, "Failed to open usage access settings: " + e2.getMessage());
                                }
                                result.put("content", "请在系统设置中开启 MIRROW 的「使用情况访问权限」：\n设置 → 安全 → 使用情况访问权限 → MIRROW → 开启");
                                result.put("success", false);
                                break;
                            }
                        }

                        long now = System.currentTimeMillis();
                        long dayStart = PerceptionClock.logicalDayStartMillis();

                        // ── 方案一：UsageEvents 算真实前台时长（抄 AionsHome 思路）──
                        // 不信任 getTotalTimeInForeground()（国产 ROM 会含后台时长）
                        // 改为从 ACTIVITY_RESUMED/PAUSED 事件时间戳自己算
                        java.util.Map<String, Long> foregroundMap = computeForegroundTimeFromEvents(usm, dayStart, now);

                        // ── 方案二：UsageEvents 没数据时回退到 getTotalTimeInForeground() ──
                        // 加上"不能超过今天已过时间"的上限保护
                        boolean fromEvents = !foregroundMap.isEmpty();
                        if (!fromEvents) {
                            Log.w(TAG, "mobile_usage: UsageEvents empty, fallback to queryUsageStats with cap");
                            java.util.List<UsageStats> stats = usm.queryUsageStats(
                                    UsageStatsManager.INTERVAL_BEST, dayStart - 86400000L, now);
                            if (stats != null) {
                                long maxPossibleMs = now - dayStart;  // 上限：今天已过时间
                                for (UsageStats s : stats) {
                                    long raw = s.getTotalTimeInForeground();
                                    long capped = Math.min(raw, maxPossibleMs);
                                    if (capped > 0) {
                                        String pkg = s.getPackageName();
                                        Long existing = foregroundMap.get(pkg);
                                        foregroundMap.put(pkg, (existing != null ? existing : 0) + capped);
                                    }
                                }
                            }
                        }

                        // queryUsageStats 的 bucket 可能含窗口外累计。事件日志不可用时，
                        // 对所有 App 的总和做物理上限归一化，避免“今天累计”超过实际经过时间。
                        long observedTotal = 0L;
                        for (Long duration : foregroundMap.values()) observedTotal += Math.max(0L, duration);
                        long maxPossibleMs = Math.max(0L, now - dayStart);
                        if (!fromEvents && observedTotal > maxPossibleMs && observedTotal > 0L) {
                            double scale = (double) maxPossibleMs / (double) observedTotal;
                            for (java.util.Map.Entry<String, Long> entry : foregroundMap.entrySet()) {
                                entry.setValue((long) (Math.max(0L, entry.getValue()) * scale));
                            }
                        }

                        // Deduplicate & sort by time
                        java.util.List<java.util.Map.Entry<String, Long>> sorted = new java.util.ArrayList<>(foregroundMap.entrySet());
                        java.util.Collections.sort(sorted, (a, b) -> Long.compare(b.getValue(), a.getValue()));

                        long totalScreenTime = 0;
                        org.json.JSONArray topApps = new org.json.JSONArray();
                        int count = 0;
                        for (java.util.Map.Entry<String, Long> entry : sorted) {
                            long ms = entry.getValue();
                            totalScreenTime += ms;
                            long minutes = ms / 60000;
                            if (minutes > 0 && count < 15) {
                                org.json.JSONObject app = new org.json.JSONObject();
                                app.put("package", entry.getKey());
                                app.put("minutes", (int) minutes);
                                topApps.put(app);
                                count++;
                            }
                        }

                        org.json.JSONObject data = new org.json.JSONObject();
                        data.put("total_minutes", (int) (totalScreenTime / 60000));
                        data.put("top_apps", topApps);
                        data.put("app_count", count);
                        data.put("source", fromEvents ? "usage_events" : "fallback_usage_stats");
                        data.put("logical_date", PerceptionClock.logicalDate());
                        data.put("window_start_ms", dayStart);
                        data.put("available", true);

                        StringBuilder sb = new StringBuilder();
                        sb.append("今日屏幕使用: ").append(totalScreenTime / 60000).append(" 分钟");
                        if (!fromEvents) sb.append(" (系统统计)");
                        sb.append("\n");
                        sb.append("最常用App:\n");
                        for (int i = 0; i < topApps.length(); i++) {
                            org.json.JSONObject a = topApps.getJSONObject(i);
                            sb.append("  ").append(i + 1).append(". ")
                              .append(a.getString("package"))
                              .append(" (").append(a.getInt("minutes")).append("分)\n");
                        }
                        result.put("data", data);
                        result.put("content", sb.toString().trim());
                        result.put("success", true);
                    } catch (Exception e) {
                        result.put("content", "使用统计失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 📱 当前前台App + 屏幕状态（抄 AionsHome 思路）──
                case "mobile_current_app": {
                    try {
                        UsageStatsManager usm = (UsageStatsManager) getContext()
                                .getSystemService(Context.USAGE_STATS_SERVICE);
                        long now = System.currentTimeMillis();
                        org.json.JSONObject data = new org.json.JSONObject();

                        // 无障碍窗口能区分“内容应用”和覆盖在其上的输入法，优先采用内容应用。
                        String pkg = MirrowAccessibilityService.getLastWindowPackage();
                        String observedWindowPkg = MirrowAccessibilityService.getObservedWindowPackage();
                        String overlayPkg = MirrowAccessibilityService.getForegroundOverlayPackage();
                        String resolution = pkg != null && !pkg.isEmpty()
                                ? (overlayPkg != null && !overlayPkg.isEmpty()
                                    ? "underlying_app_beneath_input_method"
                                    : "accessibility_window")
                                : "unknown";
                        long observedAtMs = MirrowAccessibilityService.getLastContentObservedAtMs();

                        // 回退一：UsageEvents 追踪最近 ACTIVITY_RESUMED；输入法仅作为覆盖证据。
                        String lastUsagePkg = null;
                        String usageContentPkg = null;
                        UsageEvents events = usm.queryEvents(now - 600_000, now);
                        UsageEvents.Event event = new UsageEvents.Event();
                        while (events.hasNextEvent()) {
                            events.getNextEvent(event);
                            if (event.getEventType() == UsageEvents.Event.ACTIVITY_RESUMED
                                    || event.getEventType() == 1) {
                                lastUsagePkg = event.getPackageName();
                                if (!MirrowAccessibilityService.isInputMethodPackage(
                                        getContext(), lastUsagePkg)) {
                                    usageContentPkg = lastUsagePkg;
                                }
                            }
                        }
                        if ((pkg == null || pkg.isEmpty()) && usageContentPkg != null) {
                            pkg = usageContentPkg;
                            observedWindowPkg = lastUsagePkg;
                            if (MirrowAccessibilityService.isInputMethodPackage(getContext(), lastUsagePkg)) {
                                overlayPkg = lastUsagePkg;
                                resolution = "usage_events_underlying_app_beneath_input_method";
                            } else {
                                overlayPkg = "";
                                resolution = "usage_events";
                            }
                            observedAtMs = now;
                        }

                        // 回退二：UsageEvents 无结果时回退到 lastTimeUsed，同样排除输入法。
                        if (pkg == null || pkg.isEmpty()) {
                            java.util.List<UsageStats> stats = usm.queryUsageStats(
                                    UsageStatsManager.INTERVAL_DAILY, now - 3600_000, now);
                            if (stats != null && !stats.isEmpty()) {
                                UsageStats recent = null;
                                for (UsageStats s : stats) {
                                    if (MirrowAccessibilityService.isInputMethodPackage(
                                            getContext(), s.getPackageName())) continue;
                                    if (recent == null || s.getLastTimeUsed() > recent.getLastTimeUsed()) {
                                        recent = s;
                                    }
                                }
                                if (recent != null) {
                                    pkg = recent.getPackageName();
                                    observedWindowPkg = pkg;
                                    overlayPkg = "";
                                    resolution = "usage_stats_fallback";
                                    observedAtMs = recent.getLastTimeUsed();
                                }
                            }
                        }

                        PowerManager pm = (PowerManager) getContext()
                                .getSystemService(Context.POWER_SERVICE);
                        boolean screenOn = pm != null && pm.isInteractive();
                        if (!screenOn) {
                            pkg = null;
                            observedWindowPkg = "";
                            overlayPkg = "";
                            resolution = "screen_off";
                            observedAtMs = 0L;
                            MirrowAccessibilityService.clearLastWindowState();
                        }

                        // 系统级 App 分类（Android API 26+，直接用 int 常量值兼容低 API）
                        String appCat = "other";
                        if (pkg != null && !pkg.isEmpty()) {
                            try {
                                android.content.pm.ApplicationInfo ai = getContext()
                                        .getPackageManager().getApplicationInfo(pkg, 0);
                                int c = ai.category;
                                if (c == 0) appCat = "game";       // CATEGORY_GAME
                                else if (c == 2) appCat = "video"; // CATEGORY_VIDEO
                                else if (c == 3) appCat = "social";// CATEGORY_SOCIAL
                                else if (c == 4) appCat = "social";// CATEGORY_IMAGE
                                else if (c == 5) appCat = "social";// CATEGORY_NEWS
                                else if (c == 8) appCat = "work";  // CATEGORY_PRODUCTIVITY
                            } catch (Exception e2) {
                                // 系统 App 或未安装包名，保持 other
                            }
                        }

                        // 获取应用显示名
                        String appName = "";
                        if (pkg != null && !pkg.isEmpty()) {
                            try {
                                android.content.pm.PackageManager pkgMgr = getContext().getPackageManager();
                                android.content.pm.ApplicationInfo ai = pkgMgr.getApplicationInfo(pkg, 0);
                                appName = pkgMgr.getApplicationLabel(ai).toString();
                            } catch (Exception e2) { appName = pkg; }
                        }

                        data.put("package", pkg != null ? pkg : "");
                        data.put("app_name", appName);
                        data.put("screen_on", screenOn);
                        data.put("observed_window_package", observedWindowPkg != null ? observedWindowPkg : "");
                        data.put("overlay_package", overlayPkg != null ? overlayPkg : "");
                        data.put("underlying_package", overlayPkg != null && !overlayPkg.isEmpty()
                                ? (pkg != null ? pkg : "") : "");
                        data.put("foreground_resolution", resolution);
                        data.put("observed_at_ms", observedAtMs);
                        data.put("available", true);
                        data.put("foreground_available", screenOn && pkg != null && !pkg.isEmpty());
                        result.put("data", data);
                        result.put("success", true);
                        if (pkg == null || pkg.isEmpty()) {
                            result.put("content", screenOn ? "亮屏，但暂无可确认的前台内容App" : "屏幕已关闭");
                        }
                    } catch (Exception e) {
                        result.put("success", false);
                        result.put("error", e.getMessage());
                    }
                    break;
                }

                // ── 🏥 打开华为健康 App ──
                case "mobile_open_health_app": {
                    try {
                        Intent launch = getContext().getPackageManager().getLaunchIntentForPackage("com.huawei.health");
                        if (launch == null) {
                            result.put("content", "未找到华为运动健康，请确认已安装。");
                            result.put("success", false);
                        } else {
                            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                            getContext().startActivity(launch);
                            result.put("content", "已请求打开华为健康");
                            result.put("success", true);
                        }
                    } catch (Exception e) {
                        result.put("content", "打开华为健康失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 🚶 步数传感器（累计值，不依赖华为健康）──
                case "mobile_steps_sensor": {
                    // 手动刷新标志：仅 force 时弹权限（防自动轮询骚扰）
                    boolean forceSteps = false;
                    try {
                        String pStr = call.getString("params");
                        if (pStr != null && !pStr.isEmpty()) {
                            forceSteps = new org.json.JSONObject(pStr).optBoolean("force", false);
                        }
                    } catch (Exception ignored) {}
                    try {
                        // Check ACTIVITY_RECOGNITION (Android 10+)
                        if (Build.VERSION.SDK_INT >= 29) {
                            if (ContextCompat.checkSelfPermission(getContext(),
                                    android.Manifest.permission.ACTIVITY_RECOGNITION)
                                    != PackageManager.PERMISSION_GRANTED) {
                                // 仅手动刷新且前台时弹窗
                                if (forceSteps && getActivity() != null) {
                                    androidx.core.app.ActivityCompat.requestPermissions(
                                            getActivity(),
                                            new String[]{android.Manifest.permission.ACTIVITY_RECOGNITION},
                                            203);
                                    result.put("content", "[步数:权限✗] 活动识别权限未授予，请在弹出对话框中授权。");
                                } else {
                                    result.put("content", "[步数:权限✗] 活动识别权限未授予");
                                }
                                result.put("success", false);
                                break;
                            }
                        }

                        SensorManager sm = (SensorManager) getContext().getSystemService(Context.SENSOR_SERVICE);
                        Sensor stepSensor = sm.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
                        if (stepSensor == null) {
                            result.put("content", "此设备不支持步数传感器");
                            result.put("success", false);
                            break;
                        }

                        final java.util.concurrent.CountDownLatch latch = new java.util.concurrent.CountDownLatch(1);
                        final float[] valueHolder = new float[1];
                        final SensorEventListener listener = new SensorEventListener() {
                            @Override public void onSensorChanged(SensorEvent event) {
                                valueHolder[0] = event.values[0];
                                latch.countDown();
                            }
                            @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {}
                        };

                        sm.registerListener(listener, stepSensor, SensorManager.SENSOR_DELAY_FASTEST);
                        boolean got = latch.await(5, java.util.concurrent.TimeUnit.SECONDS);
                        sm.unregisterListener(listener);

                        if (got && valueHolder[0] >= 0) {
                            int steps = (int) valueHolder[0];
                            org.json.JSONObject data = new org.json.JSONObject();
                            data.put("steps", steps);
                            data.put("source", "sensor");
                            // 只读复现今日步数（与 MirrowRelayService 共享 mirrow_config prefs，同进程同源）
                            try {
                                SharedPreferences sp = getContext().getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
                                float dayStart = sp.getFloat("step_day_start_counter", -1);
                                float rebootOffset = sp.getFloat("step_reboot_offset", 0);
                                String prefLogicalDate = sp.getString("step_logical_date", "");
                                // 5点跨天逻辑日（与 MirrowRelayService.getLogicalDate 一致）
                                java.util.Calendar cal = java.util.Calendar.getInstance();
                                if (cal.get(java.util.Calendar.HOUR_OF_DAY) < 5) cal.add(java.util.Calendar.DAY_OF_MONTH, -1);
                                String logicalDate = new java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(cal.getTime());
                                if (dayStart >= 0 && logicalDate.equals(prefLogicalDate)) {
                                    int today = Math.max((int)(steps - dayStart + rebootOffset), 0);
                                    data.put("today_steps", today);
                                    data.put("today_steps_valid", true);
                                    data.put("logical_date", logicalDate);
                                } else {
                                    data.put("today_steps_valid", false);  // 基线未初始化/逻辑日不符 → 后端兜底
                                }
                            } catch (Exception ignored) {
                                data.put("today_steps_valid", false);
                            }
                            result.put("data", data);
                            result.put("content", "步数(传感器): " + steps);
                            result.put("success", true);
                            Log.i(TAG, "mobile_steps_sensor: " + steps);
                        } else {
                            result.put("content", "步数传感器暂无数据，请走几步后重试。");
                            result.put("success", false);
                        }
                    } catch (Exception e) {
                        result.put("content", "步数传感器失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 🚶 步数（旧：含华为健康回退，保留兼容）──
                case "mobile_steps": {
                    try {
                        // Android 10+ need ACTIVITY_RECOGNITION for step sensors
                        if (Build.VERSION.SDK_INT >= 29) {
                            if (ContextCompat.checkSelfPermission(getContext(),
                                    android.Manifest.permission.ACTIVITY_RECOGNITION)
                                    != PackageManager.PERMISSION_GRANTED) {
                                if (getActivity() != null) {
                                    androidx.core.app.ActivityCompat.requestPermissions(
                                            getActivity(),
                                            new String[]{android.Manifest.permission.ACTIVITY_RECOGNITION},
                                            202);
                                }
                                result.put("content", "活动识别权限未授予，已弹出授权对话框。请授权后重新查询步数。");
                                result.put("success", false);
                                break;
                            }
                        }

                        SensorManager sm = (SensorManager) getContext().getSystemService(Context.SENSOR_SERVICE);
                        Sensor stepSensor = sm.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
                        if (stepSensor == null) {
                            result.put("content", "此设备不支持步数传感器");
                            result.put("success", false);
                            break;
                        }

                        // Use a latch to get one reading (on-change sensor, max 8s)
                        final java.util.concurrent.CountDownLatch stepLatch = new java.util.concurrent.CountDownLatch(1);
                        final float[] stepsHolder = new float[1];
                        final SensorEventListener listener = new SensorEventListener() {
                            @Override
                            public void onSensorChanged(SensorEvent event) {
                                stepsHolder[0] = event.values[0];
                                stepLatch.countDown();
                            }
                            @Override
                            public void onAccuracyChanged(Sensor sensor, int accuracy) {}
                        };

                        sm.registerListener(listener, stepSensor, SensorManager.SENSOR_DELAY_FASTEST);
                        boolean got = stepLatch.await(8, java.util.concurrent.TimeUnit.SECONDS);
                        sm.unregisterListener(listener);

                        if (got && stepsHolder[0] > 0) {
                            int steps = (int) stepsHolder[0];
                            org.json.JSONObject data = new org.json.JSONObject();
                            data.put("steps", steps);
                            result.put("data", data);
                            result.put("content", "今日步数(传感器): " + steps + " 步");
                            result.put("success", true);
                        } else {
                            result.put("content", "步数传感器暂无数据。请走几步触发传感器更新后再查询，或使用华为健康同步。");
                            result.put("success", false);
                        }
                    } catch (Exception e) {
                        result.put("content", "步数读取失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 🔔 推送通知到手机 ──
                case "mobile_notify": {
                    try {
                        String title = "MIRROW";
                        String body = "AI 发来了一条消息";
                        String deliveryId = "";
                        try {
                            String paramsStr = call.getString("params");
                            if (paramsStr != null && !paramsStr.isEmpty()) {
                                org.json.JSONObject paramsJson = new org.json.JSONObject(paramsStr);
                                title = paramsJson.optString("title", "MIRROW");
                                body = paramsJson.optString("body", "AI 发来了一条消息");
                                deliveryId = paramsJson.optString("delivery_id", "");
                            }
                        } catch (Exception ignored) {}

                        // Check notification permission (Android 13+)
                        if (Build.VERSION.SDK_INT >= 33) {
                            if (ContextCompat.checkSelfPermission(getContext(), android.Manifest.permission.POST_NOTIFICATIONS)
                                    != PackageManager.PERMISSION_GRANTED) {
                                // Request permission
                                if (getActivity() != null) {
                                    androidx.core.app.ActivityCompat.requestPermissions(
                                            getActivity(),
                                            new String[]{android.Manifest.permission.POST_NOTIFICATIONS},
                                            201);
                                }
                                result.put("content", "通知权限未授予，已弹出授权对话框");
                                result.put("success", false);
                                break;
                            }
                        }

                        // Create notification channel (Android 8+)
                        if (Build.VERSION.SDK_INT >= 26) {
                            android.app.NotificationChannel channel = new android.app.NotificationChannel(
                                    "mirrow_push",
                                    "MIRROW消息",
                                    android.app.NotificationManager.IMPORTANCE_HIGH
                            );
                            channel.setDescription("AI 的主动通知");
                            android.app.NotificationManager nm = getContext().getSystemService(android.app.NotificationManager.class);
                            if (nm != null) nm.createNotificationChannel(channel);
                        }

                        // Build intent to open MIRROW when tapped (explicit intent for reliability)
                        Intent launchIntent = null;
                        try {
                            launchIntent = getContext().getPackageManager()
                                    .getLaunchIntentForPackage(getContext().getPackageName());
                        } catch (Exception e) {
                            Log.w(TAG, "getLaunchIntentForPackage failed: " + e.getMessage());
                        }
                        if (launchIntent == null) {
                            // Fallback: explicit class intent
                            launchIntent = new Intent(getContext(), com.mirrow.app.MainActivity.class);
                        }
                        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                        android.app.PendingIntent pendingIntent = android.app.PendingIntent.getActivity(
                                getContext(), 0, launchIntent,
                                android.app.PendingIntent.FLAG_UPDATE_CURRENT | android.app.PendingIntent.FLAG_IMMUTABLE);

                        NotificationCompat.Builder builder = new NotificationCompat.Builder(getContext(), "mirrow_push")
                                .setSmallIcon(android.R.drawable.ic_dialog_info)
                                .setContentTitle(title)
                                .setContentText(body)
                                .setContentIntent(pendingIntent)
                                .setPriority(NotificationCompat.PRIORITY_HIGH)
                                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                                .setDefaults(NotificationCompat.DEFAULT_SOUND | NotificationCompat.DEFAULT_VIBRATE)
                                .setAutoCancel(true);

                        int notificationId = deliveryId.isEmpty()
                                ? (int) (System.currentTimeMillis() & 0x7FFFFFFF)
                                : (deliveryId.hashCode() & 0x7FFFFFFF);
                        NotificationManagerCompat.from(getContext()).notify(
                                "mirrow_delivery", notificationId, builder.build());
                        Log.i(TAG, "mobile_notify: sent notification title=" + title);
                        MainApplication.log("TOOL", "notify sent: " + title, null);
                        result.put("content", "通知已发送: " + title);
                        result.put("success", true);
                    } catch (Exception e) {
                        result.put("content", "通知发送失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 查岗一次性原生闹钟（前台 WebView relay 也走同一幂等实现）──
                case "mobile_alarm": {
                    try {
                        String paramsStr = call.getString("params");
                        org.json.JSONObject alarmParams = paramsStr == null || paramsStr.isEmpty()
                                ? new org.json.JSONObject() : new org.json.JSONObject(paramsStr);
                        org.json.JSONObject alarmResult = com.mirrow.app.CheckupAlarmController.schedule(getContext(), alarmParams);
                        java.util.Iterator<String> keys = alarmResult.keys();
                        while (keys.hasNext()) {
                            String key = keys.next();
                            result.put(key, alarmResult.opt(key));
                        }
                    } catch (Exception e) {
                        result.put("success", false);
                        result.put("error", "闹钟设置失败: " + e.getMessage());
                        result.put("content", "闹钟设置失败: " + e.getMessage());
                    }
                    break;
                }
                case "mobile_alarm_cancel": {
                    try {
                        String paramsStr = call.getString("params");
                        org.json.JSONObject alarmParams = paramsStr == null || paramsStr.isEmpty()
                                ? new org.json.JSONObject() : new org.json.JSONObject(paramsStr);
                        org.json.JSONObject alarmResult = com.mirrow.app.CheckupAlarmController.cancel(getContext(), alarmParams);
                        java.util.Iterator<String> keys = alarmResult.keys();
                        while (keys.hasNext()) {
                            String key = keys.next();
                            result.put(key, alarmResult.opt(key));
                        }
                    } catch (Exception e) {
                        result.put("success", false);
                        result.put("error", "闹钟取消失败: " + e.getMessage());
                        result.put("content", "闹钟取消失败: " + e.getMessage());
                    }
                    break;
                }

                // ── ❤️ BLE 心率直连（v29：分步诊断 + 冷却机制）──
                case "mobile_heart_rate": {
                    String HR_MAC = "";
                    String HR_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb";

                    // ── 手动重连标志 + 设备 MAC（后端从 SQLite/env 下发）──
                    boolean forceReconnect = false;
                    try {
                        String pStr = call.getString("params");
                        if (pStr != null && !pStr.isEmpty()) {
                            org.json.JSONObject pj = new org.json.JSONObject(pStr);
                            forceReconnect = pj.optBoolean("force", false);
                            HR_MAC = pj.optString("mac", HR_MAC);
                        }
                    } catch (Exception ignored) {}

                    try {
                        // ── 阶段1: 蓝牙权限（Android 12+）──
                        // 统一控制模型：仅手动重连(force)时弹权限；后台 auto 轮询缺权限静默失败，不弹窗骚扰
                        if (Build.VERSION.SDK_INT >= 31) {
                            boolean scanOk = ContextCompat.checkSelfPermission(getContext(),
                                android.Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
                            boolean connOk = ContextCompat.checkSelfPermission(getContext(),
                                android.Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;

                            if (!scanOk || !connOk) {
                                if (forceReconnect && getActivity() != null) {
                                    String[] perms = (!scanOk && !connOk)
                                        ? new String[]{android.Manifest.permission.BLUETOOTH_SCAN, android.Manifest.permission.BLUETOOTH_CONNECT}
                                        : new String[]{!scanOk ? android.Manifest.permission.BLUETOOTH_SCAN : android.Manifest.permission.BLUETOOTH_CONNECT};
                                    androidx.core.app.ActivityCompat.requestPermissions(getActivity(), perms,
                                        !scanOk ? 205 : 206);
                                    result.put("content", "[BLE:权限✗] 蓝牙权限未授予（扫描=" + scanOk + " 连接=" + connOk + "），请在弹出对话框中授权后重试。");
                                } else {
                                    result.put("content", "[BLE:权限✗] 蓝牙权限未授予。请点重连按钮授权，或在系统设置中开启蓝牙权限。");
                                }
                                result.put("success", false); break;
                            }
                        }

                        // ── 阶段2: 蓝牙开关 ──
                        BluetoothManager bm = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
                        BluetoothAdapter adapter = bm.getAdapter();
                        if (adapter == null) {
                            result.put("content", "[BLE:蓝牙✗] 设备不支持蓝牙。");
                            result.put("success", false); break;
                        }
                        if (!adapter.isEnabled()) {
                            if (forceReconnect && getActivity() != null) {
                                try {
                                    getActivity().startActivityForResult(
                                        new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE), 204);
                                } catch (Exception ignored) {}
                                result.put("content", "[BLE:蓝牙✗] 蓝牙未开启，请在弹出对话框中开启蓝牙后重试。");
                            } else {
                                result.put("content", "[BLE:蓝牙✗] 蓝牙未开启。请点重连按钮或手动开启蓝牙。");
                            }
                            result.put("success", false); break;
                        }

                        // ── 阶段3: GATT 连接 ──
                        BluetoothDevice device = adapter.getRemoteDevice(HR_MAC);
                        final CountDownLatch latch = new CountDownLatch(1);
                        final int[] hrVal = new int[1];
                        final String[] hrErr = new String[1];
                        final int[] gattStatus = new int[1]; // 记录 GATT status
                        final BluetoothGatt[] gattHolder = new BluetoothGatt[1];

                        gattHolder[0] = device.connectGatt(getContext(), false, new BluetoothGattCallback() {
                            public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
                                gattStatus[0] = status;
                                if (newState == BluetoothProfile.STATE_CONNECTED) {
                                    Log.d(TAG, "mobile_hr: GATT connected, discovering services...");
                                    g.discoverServices();
                                }
                                else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                                    if (hrVal[0] == 0) {
                                        if (status != 0) {
                                            hrErr[0] = "[BLE:GATT✗] 连接失败 status=" + status + "（";
                                            switch (status) {
                                                case 133: hrErr[0] += "设备不在范围或已断开"; break;
                                                case 8:   hrErr[0] += "设备超时"; break;
                                                case 22:  hrErr[0] += "设备未找到"; break;
                                                case 257: hrErr[0] += "连接超时"; break;
                                                default:  hrErr[0] += "未知错误"; break;
                                            }
                                            hrErr[0] += "）";
                                        } else {
                                            hrErr[0] = "[BLE:GATT✗] 手环已断开";
                                        }
                                    }
                                    latch.countDown();
                                }
                            }
                            public void onServicesDiscovered(BluetoothGatt g, int status) {
                                if (status != 0) {
                                    hrErr[0] = "[BLE:服务✗] 服务发现失败 status=" + status;
                                    latch.countDown(); return;
                                }
                                BluetoothGattService svc = g.getService(java.util.UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb"));
                                if (svc == null) {
                                    hrErr[0] = "[BLE:服务✗] 手环无心率服务(0x180D)";
                                    latch.countDown(); return;
                                }
                                BluetoothGattCharacteristic hrCh = svc.getCharacteristic(java.util.UUID.fromString(HR_CHAR_UUID));
                                if (hrCh == null) {
                                    hrErr[0] = "[BLE:特征✗] 心率服务无0x2A37特征值";
                                    latch.countDown(); return;
                                }
                                if ((hrCh.getProperties() & BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0) {
                                    g.setCharacteristicNotification(hrCh, true);
                                    BluetoothGattDescriptor cccd = hrCh.getDescriptor(java.util.UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"));
                                    if (cccd != null) {
                                        cccd.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                                        g.writeDescriptor(cccd);
                                    }
                                } else {
                                    g.readCharacteristic(hrCh);
                                }
                            }
                            public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic ch, int status) {
                                if (status == 0 && ch.getValue() != null && ch.getValue().length >= 2) {
                                    parseHrValue(ch.getValue(), hrVal, hrErr);
                                } else {
                                    hrErr[0] = "[BLE:读取✗] 特征值读取失败 status=" + status;
                                }
                                g.disconnect();
                            }
                            public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic ch) {
                                if (ch.getValue() != null && ch.getValue().length >= 2) {
                                    parseHrValue(ch.getValue(), hrVal, hrErr);
                                }
                                g.disconnect();
                            }
                            public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
                                // Notification enabled, wait for onCharacteristicChanged
                            }
                        });

                        boolean done = latch.await(10, TimeUnit.SECONDS);
                        try { if (gattHolder[0] != null) gattHolder[0].close(); } catch (Exception ignored) {}

                        if (hrVal[0] > 0) {
                            org.json.JSONObject data = new org.json.JSONObject();
                            data.put("heart_rate", hrVal[0]);
                            data.put("method", "ble_direct");
                            result.put("data", data);
                            result.put("content", "心率: " + hrVal[0] + " bpm (BLE直连)");
                            result.put("success", true);
                            Log.i(TAG, "mobile_heart_rate(v29): " + hrVal[0] + " bpm");
                        } else {
                            String err = hrErr[0] != null ? hrErr[0] : "[BLE:超时] 心率读取超时(10s)，手环在附近且蓝牙已开？";
                            result.put("content", err);
                            result.put("success", false);
                            Log.w(TAG, "mobile_heart_rate(v29): " + err);
                        }
                    } catch (Exception e) {
                        result.put("content", "[BLE:异常] " + e.getMessage());
                        result.put("success", false);
                        Log.e(TAG, "mobile_heart_rate(v29) exception: " + e.getMessage());
                    }
                    break;
                }

                // ── 🔍 手机已配对蓝牙设备扫描（getBondedDevices → identity address，规避随机地址）──
                case "mobile_ble_scan": {
                    boolean forceScan = false;
                    try {
                        String pStr = call.getString("params");
                        if (pStr != null && !pStr.isEmpty()) {
                            forceScan = new org.json.JSONObject(pStr).optBoolean("force", false);
                        }
                    } catch (Exception ignored) {}
                    try {
                        if (Build.VERSION.SDK_INT >= 31) {
                            boolean connOk = ContextCompat.checkSelfPermission(getContext(),
                                android.Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
                            if (!connOk) {
                                if (forceScan && getActivity() != null) {
                                    androidx.core.app.ActivityCompat.requestPermissions(getActivity(),
                                        new String[]{android.Manifest.permission.BLUETOOTH_CONNECT}, 206);
                                    result.put("content", "[BLE:权限✗] 蓝牙连接权限未授予，请在弹出对话框中授权后重试。");
                                } else {
                                    result.put("content", "[BLE:权限✗] 蓝牙连接权限未授予。");
                                }
                                result.put("success", false); break;
                            }
                        }
                        BluetoothManager bm = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
                        BluetoothAdapter adapter = bm.getAdapter();
                        if (adapter == null || !adapter.isEnabled()) {
                            result.put("content", "[BLE:蓝牙✗] 蓝牙未开启。");
                            result.put("success", false); break;
                        }
                        org.json.JSONArray arr = new org.json.JSONArray();
                        for (BluetoothDevice d : adapter.getBondedDevices()) {
                            org.json.JSONObject o = new org.json.JSONObject();
                            o.put("address", d.getAddress());
                            o.put("name", d.getName() != null ? d.getName() : "");
                            o.put("type", d.getType());
                            o.put("source", "bonded");
                            arr.put(o);
                        }
                        org.json.JSONObject data = new org.json.JSONObject();
                        data.put("devices", arr);
                        result.put("data", data);
                        result.put("content", "已配对设备 " + arr.length() + " 个");
                        result.put("success", true);
                    } catch (Exception e) {
                        result.put("content", "[BLE:异常] " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 悬浮球（刷手机模式）──
                case "show_floating_widget": {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                        && !Settings.canDrawOverlays(getContext())) {
                        android.content.Context ctx = getActivity() != null ? getActivity() : getContext();
                        FloatingWidgetService.openOverlayPermissionSettings(ctx);
                        Toast.makeText(ctx, "请开启「在其他应用上层显示」权限", Toast.LENGTH_LONG).show();
                        result.put("content", "需要悬浮窗权限，已跳转到设置页");
                        result.put("success", false);
                    } else {
                        Log.i("MIRROW_FloatingWidget", "Starting FloatingWidgetService");
                        Intent fwIntent = new Intent(getContext(), FloatingWidgetService.class);
                        ContextCompat.startForegroundService(getContext(), fwIntent);
                        result.put("content", "悬浮球已显示");
                        result.put("success", true);
                    }
                    break;
                }

                case "hide_floating_widget": {
                    Intent fwIntent = new Intent(getContext(), FloatingWidgetService.class);
                    getContext().stopService(fwIntent);
                    result.put("content", "悬浮球已隐藏");
                    result.put("success", true);
                    break;
                }

                // ── 写入后端 URL 到 SharedPreferences（供 FloatingWidgetService 读取）──
                case "write_backend_url": {
                    try {
                        String paramsStr = call.getString("params");
                        String url = DEFAULT_BACKEND_URL;
                        if (paramsStr != null && !paramsStr.isEmpty()) {
                            org.json.JSONObject p = new org.json.JSONObject(paramsStr);
                            url = p.optString("url", DEFAULT_BACKEND_URL);
                        }
                        BackendEndpointPool.save(
                            getContext(), url, new org.json.JSONArray().put(url)
                        );
                        Log.i(TAG, "write_backend_url: endpoint updated");
                        result.put("content", "Endpoint updated");
                        result.put("success", true);
                    } catch (Exception e) {
                        Log.w(TAG, "write_backend_url error: " + e.getMessage());
                        result.put("content", "写入失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                case "write_backend_endpoints": {
                    try {
                        String paramsStr = call.getString("params");
                        org.json.JSONObject p = paramsStr == null
                            ? new org.json.JSONObject()
                            : new org.json.JSONObject(paramsStr);
                        String selected = p.optString("selected_url", DEFAULT_BACKEND_URL);
                        org.json.JSONArray candidates = p.optJSONArray("candidates");
                        BackendEndpointPool.save(getContext(), selected, candidates);
                        ContextCompat.startForegroundService(
                            getContext(), new Intent(getContext(), MirrowRelayService.class)
                        );
                        Log.i(TAG, "write_backend_endpoints: endpoint pool updated");
                        result.put("content", "Endpoint pool updated");
                        result.put("success", true);
                    } catch (Exception e) {
                        Log.w(TAG, "write_backend_endpoints error: " + e.getClass().getSimpleName());
                        result.put("content", "写入失败");
                        result.put("success", false);
                    }
                    break;
                }

                case "get_backend_url": {
                    String url = BackendEndpointPool.getSelected(getContext(), DEFAULT_BACKEND_URL);
                    result.put("content", url);
                    result.put("url", url);
                    result.put("success", true);
                    break;
                }

                // ── Health Connect 健康数据读取 ──
                case "mobile_health_kit": {
                    String action = "all";
                    try {
                        String paramsStr = call.getString("params");
                        if (paramsStr != null && !paramsStr.isEmpty()) {
                            action = new org.json.JSONObject(paramsStr).optString("action", "all");
                        }
                    } catch (Exception ignored) {}

                    Log.i(TAG, "health_kit: action=" + action + " (Health Connect)");
                    MainApplication.log("TOOL", "health_kit: " + action + " (Health Connect)", null);

                    try {
                        // 检查 Health Connect SDK 是否可用
                        int sdkStatus = androidx.health.connect.client.HealthConnectClient.getSdkStatus(getContext());
                        if (sdkStatus != androidx.health.connect.client.HealthConnectClient.SDK_AVAILABLE) {
                            Log.w(TAG, "health_kit: Health Connect SDK status=" + sdkStatus);
                            if (sdkStatus == androidx.health.connect.client.HealthConnectClient.SDK_UNAVAILABLE) {
                                // 未安装 Health Connect
                                result.put("content", "[Health Connect] 未安装 Health Connect 应用。OPPO 手机请从 Play 商店下载 Health Connect。");
                            } else {
                                // SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED — 已安装但版本太旧
                                try {
                                    Intent updateIntent = new Intent(Intent.ACTION_VIEW);
                                    updateIntent.setData(android.net.Uri.parse(
                                        "https://play.google.com/store/apps/details?id=com.google.android.apps.healthdata"));
                                    updateIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                                    getContext().startActivity(updateIntent);
                                    result.put("content", "[Health Connect] Health Connect 版本过旧，已跳转 Play 商店——更新后重试。");
                                } catch (Exception e) {
                                    result.put("content", "[Health Connect] Health Connect 版本过旧，请在 Play 商店搜索更新。");
                                }
                            }
                            result.put("success", false);
                            break;
                        }

                        androidx.health.connect.client.HealthConnectClient hcClient =
                            androidx.health.connect.client.HealthConnectClient.getOrCreate(getContext());

                        // 检查权限（getGrantedPermissions 是 suspend 函数）
                        java.util.Set<String> neededPerms = new java.util.HashSet<>();
                        if (action.equals("sleep") || action.equals("all"))
                            neededPerms.add("android.permission.health.READ_SLEEP");
                        if (action.equals("steps") || action.equals("all"))
                            neededPerms.add("android.permission.health.READ_STEPS");
                        if (action.equals("heart_rate") || action.equals("all"))
                            neededPerms.add("android.permission.health.READ_HEART_RATE");
                        if (action.equals("spo2") || action.equals("all"))
                            neededPerms.add("android.permission.health.READ_OXYGEN_SATURATION");

                        java.util.Set<String> grantedPerms = kotlinx.coroutines.BuildersKt.runBlocking(
                            kotlin.coroutines.EmptyCoroutineContext.INSTANCE,
                            (kotlin.jvm.functions.Function2<kotlinx.coroutines.CoroutineScope, kotlin.coroutines.Continuation<? super java.util.Set<String>>, Object>)
                                (scope, cont) -> hcClient.getPermissionController().getGrantedPermissions(cont)
                        );
                        if (grantedPerms == null) grantedPerms = java.util.Collections.emptySet();
                        neededPerms.removeAll(grantedPerms);

                        if (!neededPerms.isEmpty()) {
                            Log.i(TAG, "health_kit: missing permissions: " + neededPerms);
                            try {
                                Intent permIntent = new Intent("android.health.connect.action.MANAGE_HEALTH_PERMISSIONS");
                                permIntent.setPackage("com.google.android.apps.healthdata");
                                permIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                                int sdkInt = Build.VERSION.SDK_INT;
                                if (sdkInt >= 35) {
                                    permIntent.putStringArrayListExtra(
                                        "health_permissions",
                                        new java.util.ArrayList<>(neededPerms)
                                    );
                                }
                                getContext().startActivity(permIntent);
                                result.put("content", "[Health Connect] 需要授权: " + String.join(", ", neededPerms)
                                    + "。已打开权限设置页，请授权后重试。");
                            } catch (Exception permEx) {
                                Log.w(TAG, "health_kit: failed to open permissions: " + permEx.getClass().getSimpleName()
                                    + ": " + permEx.getMessage());
                                result.put("content", "[Health Connect] 缺少权限: " + String.join(", ", neededPerms)
                                    + "。请在系统设置 → Health Connect → 应用权限中手动授权。(" + permEx.getClass().getSimpleName() + ")");
                            }
                            result.put("success", false);
                            result.put("missing_permissions", new org.json.JSONArray(new java.util.ArrayList<>(neededPerms)));
                            break;
                        }

                        // 读取数据
                        org.json.JSONObject data = readHealthConnectData(hcClient, action);
                        if (data != null && data.length() > 0) {
                            String content = formatHealthResult(action, data);
                            result.put("data", data);
                            result.put("success", true);
                            result.put("content", "[Health Connect] " + content);
                            result.put("source", "health_connect");
                            Log.i(TAG, "health_kit: OK → " + content);
                        } else {
                            result.put("content", "[Health Connect] 未读取到" + action + "数据。请确认华为健康数据已同步到 Health Connect。");
                            result.put("success", false);
                        }
                    } catch (Throwable e) {
                        Log.w(TAG, "health_kit error: " + e.getMessage(), e);
                        MainApplication.log("TOOL", "health_kit ERROR: " + e.getMessage(), e);
                        result.put("content", "[Health Connect] 异常: " + e.getClass().getSimpleName() + ": " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 🔥 甜焰 BLE 中继（TianYan 55 协议，2026-06-24） ──
                case "mobile_toy2": {
                    new Thread(() -> executeToy2(call), "mirrow-toy2-plugin").start();
                    return;
                }

                // ── 🎵 手机网易云 App 控制 ──
                case "mobile_open_netease":
                case "mobile_music_control": {
                    String paramsStr = call.getString("params");
                    org.json.JSONObject musicParams = paramsStr == null || paramsStr.isEmpty()
                            ? new org.json.JSONObject() : new org.json.JSONObject(paramsStr);
                    if ("mobile_open_netease".equals(tool) && !musicParams.has("action")) {
                        musicParams.put("action", "open");
                    }
                    org.json.JSONObject musicResult = com.mirrow.app.NeteaseMusicController.execute(
                            getContext(), musicParams);
                    java.util.Iterator<String> musicKeys = musicResult.keys();
                    while (musicKeys.hasNext()) {
                        String key = musicKeys.next();
                        result.put(key, musicResult.opt(key));
                    }
                    break;
                }

                // ── 将聊天图片保存到系统相册 Pictures/MIRROW ──
                case "save_image": {
                    String paramsStr = call.getString("params");
                    org.json.JSONObject p = new org.json.JSONObject(paramsStr == null ? "{}" : paramsStr);
                    String dataUrl = p.optString("data_url", "");
                    String requestedName = p.optString("filename", "");
                    int comma = dataUrl.indexOf(',');
                    if (!dataUrl.startsWith("data:image/") || comma < 0) {
                        throw new IllegalArgumentException("只允许保存已加载的图片数据");
                    }
                    String header = dataUrl.substring(5, comma);
                    String mime = header.split(";", 2)[0].toLowerCase(java.util.Locale.ROOT);
                    if (!(mime.equals("image/png") || mime.equals("image/jpeg")
                            || mime.equals("image/webp") || mime.equals("image/gif"))) {
                        throw new IllegalArgumentException("不支持的图片格式");
                    }
                    byte[] bytes = Base64.decode(dataUrl.substring(comma + 1), Base64.DEFAULT);
                    if (bytes.length == 0 || bytes.length > 25 * 1024 * 1024) {
                        throw new IllegalArgumentException("图片为空或超过 25MB");
                    }
                    String ext = mime.equals("image/jpeg") ? ".jpg"
                        : mime.equals("image/webp") ? ".webp"
                        : mime.equals("image/gif") ? ".gif" : ".png";
                    String filename = requestedName.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
                    if (filename.isEmpty()) filename = "MIRROW_" + System.currentTimeMillis() + ext;
                    if (!filename.toLowerCase(java.util.Locale.ROOT).matches(".*\\.(png|jpe?g|webp|gif)$")) {
                        filename += ext;
                    }
                    if (filename.length() > 120) filename = filename.substring(filename.length() - 120);

                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Images.Media.DISPLAY_NAME, filename);
                    values.put(MediaStore.Images.Media.MIME_TYPE, mime);
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/MIRROW");
                        values.put(MediaStore.Images.Media.IS_PENDING, 1);
                    } else {
                        File dir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES), "MIRROW");
                        if (!dir.exists() && !dir.mkdirs()) throw new IllegalStateException("无法创建相册目录");
                        values.put(MediaStore.Images.Media.DATA, new File(dir, filename).getAbsolutePath());
                    }
                    Uri uri = getContext().getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
                    if (uri == null) throw new IllegalStateException("系统相册拒绝创建图片");
                    boolean complete = false;
                    try (OutputStream output = getContext().getContentResolver().openOutputStream(uri)) {
                        if (output == null) throw new IllegalStateException("无法写入系统相册");
                        output.write(bytes);
                        output.flush();
                        complete = true;
                    } finally {
                        if (!complete) getContext().getContentResolver().delete(uri, null, null);
                    }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        ContentValues ready = new ContentValues();
                        ready.put(MediaStore.Images.Media.IS_PENDING, 0);
                        getContext().getContentResolver().update(uri, ready, null, null);
                    }
                    result.put("content", "已保存到 Pictures/MIRROW/" + filename);
                    result.put("filename", filename);
                    result.put("success", true);
                    break;
                }

                // ── 📶 WiFi 状态 ──
                case "mobile_wifi_status": {
                    try {
                        android.net.wifi.WifiManager wm = (android.net.wifi.WifiManager)
                            getContext().getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                        org.json.JSONObject wifiData = new org.json.JSONObject();
                        if (wm != null) {
                            android.net.wifi.WifiInfo wi = wm.getConnectionInfo();
                            if (wi != null && wi.getNetworkId() != -1) {
                                String ssid = wi.getSSID();
                                String bssid = wi.getBSSID();
                                wifiData.put("ssid", ssid != null ? ssid : "");
                                wifiData.put("bssid", bssid != null ? bssid : "");
                                wifiData.put("connected", true);
                                wifiData.put("signal_strength", wi.getRssi());
                                int freq = wi.getFrequency();
                                wifiData.put("is_5g", freq > 5000);
                                int ip = wi.getIpAddress();
                                if (ip != 0) {
                                    wifiData.put("ip_address", String.format("%d.%d.%d.%d",
                                        (ip & 0xff), (ip >> 8 & 0xff), (ip >> 16 & 0xff), (ip >> 24 & 0xff)));
                                }
                            } else {
                                wifiData.put("connected", false);
                                wifiData.put("ssid", org.json.JSONObject.NULL);
                                wifiData.put("bssid", org.json.JSONObject.NULL);
                            }
                        } else {
                            wifiData.put("connected", false);
                            wifiData.put("error", "WifiManager unavailable");
                        }
                        result.put("data", wifiData);
                        result.put("success", true);
                    } catch (Exception e) {
                        result.put("success", false);
                        result.put("error", e.getMessage());
                    }
                    break;
                }

                // ── 📍 GPS 定位 ──
                case "mobile_location": {
                    // ── 手动刷新标志（force=true 时弹权限 + 请求新鲜定位）──
                    boolean forceLoc = false;
                    try {
                        String pStr = call.getString("params");
                        if (pStr != null && !pStr.isEmpty()) {
                            forceLoc = new org.json.JSONObject(pStr).optBoolean("force", false);
                        }
                    } catch (Exception ignored) {}

                    try {
                        android.location.LocationManager lm = (android.location.LocationManager)
                            getContext().getSystemService(Context.LOCATION_SERVICE);

                        // Check permission
                        if (ContextCompat.checkSelfPermission(getContext(),
                                android.Manifest.permission.ACCESS_FINE_LOCATION)
                                != PackageManager.PERMISSION_GRANTED) {
                            // 仅手动刷新且前台时弹窗（防自动轮询骚扰）
                            if (forceLoc && getActivity() != null) {
                                androidx.core.app.ActivityCompat.requestPermissions(getActivity(),
                                    new String[]{android.Manifest.permission.ACCESS_FINE_LOCATION}, 207);
                                result.put("content", "[GPS:权限✗] 定位权限未授予，请在弹出对话框中授权后重试。");
                            } else {
                                result.put("content", "[GPS:权限✗] 定位权限未授予");
                            }
                            result.put("success", false);
                            break;
                        }

                        // Try GPS first, fall back to network
                        android.location.Location loc = lm.getLastKnownLocation(
                            android.location.LocationManager.GPS_PROVIDER);
                        String source = "gps";

                        if (loc == null) {
                            loc = lm.getLastKnownLocation(
                                android.location.LocationManager.NETWORK_PROVIDER);
                            source = "network";
                        }

                        // 手动刷新且无缓存 → 主动请求一次新鲜定位（阻塞最多8s）
                        if ((loc == null || (System.currentTimeMillis() - loc.getTime()) > 300_000) && forceLoc) {
                            final android.location.Location[] fresh = {null};
                            final java.util.concurrent.CountDownLatch latch = new java.util.concurrent.CountDownLatch(1);
                            android.location.LocationListener oneShot = new android.location.LocationListener() {
                                @Override public void onLocationChanged(android.location.Location l) { fresh[0] = l; latch.countDown(); }
                                @Override public void onProviderDisabled(String p) {}
                                @Override public void onProviderEnabled(String p) {}
                                @Override public void onStatusChanged(String p, int s, android.os.Bundle b) {}
                            };
                            try {
                                android.os.Looper looper = android.os.Looper.getMainLooper();
                                lm.requestSingleUpdate(android.location.LocationManager.NETWORK_PROVIDER, oneShot, looper);
                                lm.requestSingleUpdate(android.location.LocationManager.GPS_PROVIDER, oneShot, looper);
                                latch.await(8, java.util.concurrent.TimeUnit.SECONDS);
                                if (fresh[0] != null) { loc = fresh[0]; source = "fresh"; }
                            } catch (Exception ignored) {}
                        }

                        if (loc == null) {
                            result.put("content", "[GPS:无数据] 无缓存定位，请打开地图App获取位置后重试");
                            result.put("success", false);
                            break;
                        }

                        // 自动轮询时拒绝过期数据；手动刷新已尝试拿新鲜值，放宽到能用就用
                        long ageMs = System.currentTimeMillis() - loc.getTime();
                        if (ageMs > 300_000 && !forceLoc) {
                            result.put("content", "[GPS:过期] 定位数据已过期(" + (ageMs / 60000) + "分钟前)");
                            result.put("success", false);
                            break;
                        }

                        org.json.JSONObject locData = new org.json.JSONObject();
                        locData.put("lat", loc.getLatitude());
                        locData.put("lng", loc.getLongitude());
                        locData.put("accuracy", loc.getAccuracy());
                        locData.put("source", source);
                        locData.put("timestamp", new java.text.SimpleDateFormat(
                            "yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.US).format(new java.util.Date(
                            loc.getTime())));

                        result.put("data", locData);  // 对象，与 steps/HR 一致（后端 .get("lat") 需要 dict）
                        result.put("content", source + " " + String.format("%.4f,%.4f (±%.0fm)",
                            loc.getLatitude(), loc.getLongitude(), loc.getAccuracy()));
                        result.put("success", true);

                        Log.i(TAG, "mobile_location: " + result.get("content"));
                        MainApplication.log("GPS", result.get("content").toString(), null);
                    } catch (Exception e) {
                        Log.e(TAG, "mobile_location error: " + e.getMessage(), e);
                        result.put("content", "定位失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                // ── 🎵 当前歌曲（AccessibilityService 读通知栏，音乐耳蜗）──
                case "mobile_now_playing": {
                    try {
                        org.json.JSONObject request = new org.json.JSONObject().put("action", "now_playing");
                        org.json.JSONObject musicResult = com.mirrow.app.NeteaseMusicController.execute(
                                getContext(), request);
                        java.util.Iterator<String> keys = musicResult.keys();
                        while (keys.hasNext()) {
                            String key = keys.next();
                            result.put(key, musicResult.opt(key));
                        }
                    } catch (Exception e) {
                        result.put("content", "获取歌曲信息失败: " + e.getMessage());
                        result.put("success", false);
                    }
                    break;
                }

                default: {
                    Log.w(TAG, "Unknown tool: " + tool);
                    MainApplication.log("TOOL", "Unknown: " + tool, null);
                    result.put("content", "[手机工具] " + tool + " — 暂不支持");
                    result.put("success", false);
                    break;
                }
            }
        } catch (Throwable e) {
            Log.e(TAG, "execute " + tool + " error: " + e.getMessage(), e);
            MainApplication.log("TOOL", "execute " + tool + " CRASH: " + e.getMessage(), e);
            result.put("content", "[异常] " + tool + ": " + e.getClass().getSimpleName() + ": " + e.getMessage());
            result.put("success", false);
        }

        call.resolve(result);
    }

    private void executeToy2(PluginCall call) {
        JSObject result = new JSObject();
        String action = "stretch";
        int mode = 1;
        int intensity = 0;
        boolean heat = false;
        try {
            String paramsText = call.getString("params", "{}");
            org.json.JSONObject params = new org.json.JSONObject(paramsText);
            action = params.optString("action", "stretch");
            mode = params.optInt("mode", 1);
            intensity = params.optInt("intensity", 0);
            heat = params.optBoolean("heat", false);
            int vibrateIntensity = params.optInt("vibrate_intensity", 0);
            String mac = params.optString("mac", null);
            if (mac != null && !mac.isEmpty()) BLEController.setTianyanMac(mac);
            String bleJson = getToy2Ble().tianyanApply(action, mode, intensity, heat, vibrateIntensity);
            org.json.JSONObject parsed = new org.json.JSONObject(bleJson);
            result.put("success", parsed.optBoolean("success", false));
            result.put("content", parsed.optString("content", parsed.optString("error", "BLE error")));
        } catch (Exception e) {
            Log.e(TAG, "mobile_toy2 BLE error", e);
            result.put("success", false);
            result.put("content", "BLE error: " + e.getClass().getSimpleName());
        }
        MainApplication.log("TOY2", action + " m=" + mode + " i=" + intensity
                + (heat ? " +heat" : "") + " → " + result.optString("content"), null);
        call.resolve(result);
    }


    /**
     * 将健康源返回的结构化数据格式化为可读结果。
     */
    private String formatHealthResult(String action, org.json.JSONObject data) {
        try {
            if (action.equals("steps")) {
                org.json.JSONObject s = data.optJSONObject("steps");
                return "今日步数: " + (s != null ? s.optString("value", "?") : "?") + " 步";
            } else if (action.equals("sleep")) {
                org.json.JSONObject s = data.optJSONObject("sleep");
                if (s == null) return "睡眠数据: 无记录";
                java.util.List<String> parts = new java.util.ArrayList<>();
                if (s.optInt("deep_min", 0) > 0) parts.add("深睡" + s.getInt("deep_min") + "min");
                if (s.optInt("shallow_min", 0) > 0) parts.add("浅睡" + s.getInt("shallow_min") + "min");
                if (s.optInt("rem_min", 0) > 0) parts.add("REM" + s.getInt("rem_min") + "min");
                if (s.optInt("awake_min", 0) > 0) parts.add("清醒" + s.getInt("awake_min") + "min");
                return "昨晚睡眠: " + (parts.isEmpty() ? "无分期数据" : String.join(", ", parts));
            } else if (action.equals("heart_rate")) {
                org.json.JSONObject hr = data.optJSONObject("heart_rate");
                return "心率: " + (hr != null ? hr.optString("value", "?") : "?") + " bpm";
            } else if (action.equals("spo2")) {
                org.json.JSONObject sp = data.optJSONObject("spo2");
                return "血氧: " + (sp != null ? sp.optString("value", "?") : "?") + "%";
            } else {
                // all_health
                StringBuilder sb = new StringBuilder("健康数据汇总:");
                String[] keys = {"heart_rate", "steps", "spo2", "sleep"};
                for (String k : keys) {
                    if (data.has(k)) {
                        org.json.JSONObject item = data.optJSONObject(k);
                        if (item != null) {
                            if (k.equals("heart_rate")) sb.append(" 心率:").append(item.optString("value", "?")).append("bpm");
                            else if (k.equals("steps")) sb.append(" 步数:").append(item.optString("value", "?")).append("步");
                            else if (k.equals("spo2")) sb.append(" 血氧:").append(item.optString("value", "?")).append("%");
                            else if (k.equals("sleep")) {
                                sb.append(" 睡眠:");
                                if (item.optInt("deep_min", 0) > 0) sb.append("深睡").append(item.getInt("deep_min")).append("min ");
                                if (item.optInt("shallow_min", 0) > 0) sb.append("浅睡").append(item.getInt("shallow_min")).append("min");
                            }
                        }
                    }
                }
                return sb.toString();
            }
        } catch (Exception e) {
            return "健康数据解析失败: " + e.getMessage();
        }
    }

    // ─── BLE 心率值解析 ──────────────────────────────────────

    private static void parseHrValue(byte[] data, int[] hrVal, String[] hrErr) {
        int flags = data[0] & 0xFF;
        int hr;
        if ((flags & 0x01) != 0) hr = ((data[1] & 0xFF) | ((data[2] & 0xFF) << 8));
        else hr = data[1] & 0xFF;
        if (hr >= 30 && hr <= 250) hrVal[0] = hr;
        else hrErr[0] = "心率值异常: " + hr;
    }

    /**
     * 从 Google Health Connect 读取健康数据。
     * 返回结构化健康数据，供后端和 formatHealthResult 使用。
     */
    private org.json.JSONObject readHealthConnectData(
            androidx.health.connect.client.HealthConnectClient hcClient, String action) throws Exception {

        org.json.JSONObject data = new org.json.JSONObject();
        ZoneId zone = ZoneId.systemDefault();
        LocalDate today = LocalDate.now(zone);
        Instant now = Instant.now();

        // ── 睡眠：昨天 18:00 ~ 今天 12:00（覆盖典型睡眠窗口）──
        if (action.equals("sleep") || action.equals("all")) {
            try {
                Instant sleepStart = today.minusDays(1).atTime(18, 0).atZone(zone).toInstant();
                Instant sleepEnd = today.atTime(12, 0).atZone(zone).toInstant();

                // 构造 ReadRecordsRequest（Kotlin 数据类，直接调构造器）
                androidx.health.connect.client.request.ReadRecordsRequest<androidx.health.connect.client.records.SleepSessionRecord>
                    sleepReq = new androidx.health.connect.client.request.ReadRecordsRequest<>(
                        kotlin.jvm.JvmClassMappingKt.getKotlinClass(
                            androidx.health.connect.client.records.SleepSessionRecord.class),
                        androidx.health.connect.client.time.TimeRangeFilter.between(sleepStart, sleepEnd),
                        null, false, 10, null);

                // readRecords 是 suspend 函数，用 runBlocking 桥接
                @SuppressWarnings("unchecked")
                java.util.List<androidx.health.connect.client.records.SleepSessionRecord> sessions =
                    ((androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.SleepSessionRecord>)
                        kotlinx.coroutines.BuildersKt.runBlocking(
                            kotlin.coroutines.EmptyCoroutineContext.INSTANCE,
                            (kotlin.jvm.functions.Function2<kotlinx.coroutines.CoroutineScope, kotlin.coroutines.Continuation<? super androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.SleepSessionRecord>>, Object>)
                                (scope, cont) -> hcClient.readRecords(sleepReq, cont)
                        )).getRecords();
                Log.i(TAG, "health_kit: found " + sessions.size() + " sleep session(s)");

                if (!sessions.isEmpty()) {
                    androidx.health.connect.client.records.SleepSessionRecord session = sessions.get(sessions.size() - 1);

                    Instant sStart = session.getStartTime();
                    Instant sEnd = session.getEndTime();
                    long totalMin = Duration.between(sStart, sEnd).toMinutes();

                    org.json.JSONObject sleepObj = new org.json.JSONObject();
                    sleepObj.put("total_min", (int) totalMin);
                    sleepObj.put("start_time", sStart.toString());
                    sleepObj.put("end_time", sEnd.toString());

                    // 从 SleepSessionRecord.getStages() 获取睡眠阶段
                    java.util.List<androidx.health.connect.client.records.SleepSessionRecord.Stage> stages =
                        session.getStages();
                    if (stages != null && !stages.isEmpty()) {
                        Log.i(TAG, "health_kit: found " + stages.size() + " sleep stage(s)");
                        int deepMin = 0, shallowMin = 0, remMin = 0, awakeMin = 0;
                        for (androidx.health.connect.client.records.SleepSessionRecord.Stage stage : stages) {
                            long durMin = Duration.between(
                                stage.getStartTime(), stage.getEndTime()).toMinutes();
                            int st = stage.getStage();
                            if (st == androidx.health.connect.client.records.SleepSessionRecord.STAGE_TYPE_DEEP) {
                                deepMin += (int) durMin;
                            } else if (st == androidx.health.connect.client.records.SleepSessionRecord.STAGE_TYPE_LIGHT) {
                                shallowMin += (int) durMin;
                            } else if (st == androidx.health.connect.client.records.SleepSessionRecord.STAGE_TYPE_REM) {
                                remMin += (int) durMin;
                            } else if (st == androidx.health.connect.client.records.SleepSessionRecord.STAGE_TYPE_AWAKE) {
                                awakeMin += (int) durMin;
                            }
                        }
                        if (deepMin > 0) sleepObj.put("deep_min", deepMin);
                        if (shallowMin > 0) sleepObj.put("shallow_min", shallowMin);
                        if (remMin > 0) sleepObj.put("rem_min", remMin);
                        if (awakeMin > 0) sleepObj.put("awake_min", awakeMin);
                    }
                    data.put("sleep", sleepObj);
                }
            } catch (Exception sleepEx) {
                Log.w(TAG, "health_kit: sleep read failed: " + sleepEx.getMessage());
            }
        }

        // ── 步数：今日 00:00 ~ now ──
        if (action.equals("steps") || action.equals("all")) {
            try {
                Instant stepsStart = today.atStartOfDay(zone).toInstant();

                androidx.health.connect.client.request.ReadRecordsRequest<androidx.health.connect.client.records.StepsRecord>
                    stepsReq = new androidx.health.connect.client.request.ReadRecordsRequest<>(
                        kotlin.jvm.JvmClassMappingKt.getKotlinClass(
                            androidx.health.connect.client.records.StepsRecord.class),
                        androidx.health.connect.client.time.TimeRangeFilter.between(stepsStart, now),
                        null, false, 500, null);

                @SuppressWarnings("unchecked")
                java.util.List<androidx.health.connect.client.records.StepsRecord> stepRecords =
                    ((androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.StepsRecord>)
                        kotlinx.coroutines.BuildersKt.runBlocking(
                            kotlin.coroutines.EmptyCoroutineContext.INSTANCE,
                            (kotlin.jvm.functions.Function2<kotlinx.coroutines.CoroutineScope, kotlin.coroutines.Continuation<? super androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.StepsRecord>>, Object>)
                                (scope, cont) -> hcClient.readRecords(stepsReq, cont)
                        )).getRecords();

                long totalSteps = 0;
                for (androidx.health.connect.client.records.StepsRecord sr : stepRecords) {
                    totalSteps += sr.getCount();
                }

                if (totalSteps > 0) {
                    data.put("steps", new org.json.JSONObject().put("value", (int) totalSteps));
                    Log.i(TAG, "health_kit: steps=" + totalSteps + " (from " + stepRecords.size() + " records)");
                }
            } catch (Exception stepsEx) {
                Log.w(TAG, "health_kit: steps read failed: " + stepsEx.getMessage());
            }
        }

        // ── 心率：最近一次测量（过去 1 小时内）──
        if (action.equals("heart_rate") || action.equals("all")) {
            try {
                Instant hrStart = now.minus(Duration.ofHours(1));

                androidx.health.connect.client.request.ReadRecordsRequest<androidx.health.connect.client.records.HeartRateRecord>
                    hrReq = new androidx.health.connect.client.request.ReadRecordsRequest<>(
                        kotlin.jvm.JvmClassMappingKt.getKotlinClass(
                            androidx.health.connect.client.records.HeartRateRecord.class),
                        androidx.health.connect.client.time.TimeRangeFilter.between(hrStart, now),
                        null, false, 3, null);

                @SuppressWarnings("unchecked")
                java.util.List<androidx.health.connect.client.records.HeartRateRecord> hrRecords =
                    ((androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.HeartRateRecord>)
                        kotlinx.coroutines.BuildersKt.runBlocking(
                            kotlin.coroutines.EmptyCoroutineContext.INSTANCE,
                            (kotlin.jvm.functions.Function2<kotlinx.coroutines.CoroutineScope, kotlin.coroutines.Continuation<? super androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.HeartRateRecord>>, Object>)
                                (scope, cont) -> hcClient.readRecords(hrReq, cont)
                        )).getRecords();

                if (!hrRecords.isEmpty()) {
                    androidx.health.connect.client.records.HeartRateRecord latestHr =
                        hrRecords.get(hrRecords.size() - 1);
                    java.util.List<androidx.health.connect.client.records.HeartRateRecord.Sample> samples =
                        latestHr.getSamples();
                    if (!samples.isEmpty()) {
                        long bpm = samples.get(samples.size() - 1).getBeatsPerMinute();
                        data.put("heart_rate", new org.json.JSONObject().put("value", (int) bpm));
                        Log.i(TAG, "health_kit: hr=" + bpm);
                    }
                }
            } catch (Exception hrEx) {
                Log.w(TAG, "health_kit: heart_rate read failed: " + hrEx.getMessage());
            }
        }

        // ── 血氧：最近一次测量（过去 1 小时内）──
        if (action.equals("spo2") || action.equals("all")) {
            try {
                Instant spStart = now.minus(Duration.ofHours(1));

                androidx.health.connect.client.request.ReadRecordsRequest<androidx.health.connect.client.records.OxygenSaturationRecord>
                    spReq = new androidx.health.connect.client.request.ReadRecordsRequest<>(
                        kotlin.jvm.JvmClassMappingKt.getKotlinClass(
                            androidx.health.connect.client.records.OxygenSaturationRecord.class),
                        androidx.health.connect.client.time.TimeRangeFilter.between(spStart, now),
                        null, false, 3, null);

                @SuppressWarnings("unchecked")
                java.util.List<androidx.health.connect.client.records.OxygenSaturationRecord> spRecords =
                    ((androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.OxygenSaturationRecord>)
                        kotlinx.coroutines.BuildersKt.runBlocking(
                            kotlin.coroutines.EmptyCoroutineContext.INSTANCE,
                            (kotlin.jvm.functions.Function2<kotlinx.coroutines.CoroutineScope, kotlin.coroutines.Continuation<? super androidx.health.connect.client.response.ReadRecordsResponse<androidx.health.connect.client.records.OxygenSaturationRecord>>, Object>)
                                (scope, cont) -> hcClient.readRecords(spReq, cont)
                        )).getRecords();

                if (!spRecords.isEmpty()) {
                    androidx.health.connect.client.records.OxygenSaturationRecord latestSp =
                        spRecords.get(spRecords.size() - 1);
                    double pct = latestSp.getPercentage().getValue();
                    data.put("spo2", new org.json.JSONObject().put("value", (int) Math.round(pct)));
                    Log.i(TAG, "health_kit: spo2=" + pct + "%");
                }
            } catch (Exception spEx) {
                Log.w(TAG, "health_kit: spo2 read failed: " + spEx.getMessage());
            }
        }

        return data;
    }

    /**
     * 从 UsageEvents 事件日志计算每个 App 的真实前台时长。
     * 抄 AionsHome 思路：追踪 ACTIVITY_RESUMED → ACTIVITY_PAUSED/STOPPED/MOVE_TO_BACKGROUND
     * 转换，用事件时间戳差值自己算，不依赖 getTotalTimeInForeground()（国产 ROM 不靠谱）。
     *
     * @param usm   UsageStatsManager
     * @param start 起始时间戳 (ms)
     * @param end   结束时间戳 (ms)
     * @return 包名 → 前台时长(ms) 的映射
     */
    private java.util.Map<String, Long> computeForegroundTimeFromEvents(
            UsageStatsManager usm, long start, long end) {
        java.util.Map<String, Long> result = new java.util.LinkedHashMap<>();
        try {
            UsageEvents events = usm.queryEvents(start, end);
            UsageEvents.Event event = new UsageEvents.Event();

            String currentPkg = null;
            long currentStart = 0;

            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                String pkg = event.getPackageName();
                if (pkg == null) continue;
                int et = event.getEventType();

                // 进入前台：ACTIVITY_RESUMED(=1 or =2) 或 MOVE_TO_FOREGROUND(=19, API 29+)
                if (et == UsageEvents.Event.ACTIVITY_RESUMED || et == 1
                        || et == UsageEvents.Event.MOVE_TO_FOREGROUND) {
                    // 关掉上一个 App 的计时
                    if (currentPkg != null && currentStart > 0) {
                        long dur = event.getTimeStamp() - currentStart;
                        if (dur > 0) {
                            Long ex = result.get(currentPkg);
                            result.put(currentPkg, (ex != null ? ex : 0) + dur);
                        }
                    }
                    currentPkg = pkg;
                    currentStart = event.getTimeStamp();
                }
                // 退出前台：ACTIVITY_PAUSED/STOPPED 或 MOVE_TO_BACKGROUND(=20, API 29+)
                else if (et == UsageEvents.Event.ACTIVITY_PAUSED
                        || et == UsageEvents.Event.ACTIVITY_STOPPED
                        || et == UsageEvents.Event.MOVE_TO_BACKGROUND) {
                    if (currentPkg != null && currentPkg.equals(pkg) && currentStart > 0) {
                        long dur = event.getTimeStamp() - currentStart;
                        if (dur > 0) {
                            Long ex = result.get(currentPkg);
                            result.put(currentPkg, (ex != null ? ex : 0) + dur);
                        }
                        currentPkg = null;
                        currentStart = 0;
                    }
                }
            }

            // 当前还在前台的 App（从最后一次 RESUMED 到现在）
            if (currentPkg != null && currentStart > 0) {
                long dur = end - currentStart;
                if (dur > 0) {
                    Long ex = result.get(currentPkg);
                    result.put(currentPkg, (ex != null ? ex : 0) + dur);
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "computeForegroundTimeFromEvents error: " + e.getMessage());
        }
        return result;
    }

    // ── SharedPreferences 工具（供 FloatingWidgetService 读取后端 URL）──

    /**
     * 获取后端 URL，优先从 SharedPreferences 读取（JS 启动时写入），无则回退默认值。
     */
    public static String getBackendUrl(Context context) {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getString(KEY_BACKEND_URL, DEFAULT_BACKEND_URL);
    }

}
