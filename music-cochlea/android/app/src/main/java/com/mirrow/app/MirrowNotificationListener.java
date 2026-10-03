package com.mirrow.app;
import android.service.notification.NotificationListenerService;
/** Permission carrier for NetEase MediaSession access; no notification bodies are exported. */
public final class MirrowNotificationListener extends NotificationListenerService {
    @Override public void onListenerConnected() { NeteasePlaybackGuard.restore(this); }
}
