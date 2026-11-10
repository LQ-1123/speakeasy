package com.speakeasy.app;

import android.content.Context;
import android.content.Intent;
import android.util.Log;

import androidx.core.app.NotificationManagerCompat;

import cn.jpush.android.api.JPushInterface;
import cn.jpush.android.api.NotificationMessage;
import cn.jpush.android.service.JPushMessageReceiver;

import org.json.JSONObject;

/**
 * JPush message/lifecycle receiver.
 *
 * <p>All dedupe state lives in {@link PushEvents} so the "seen / arrived /
 * displayed / read" decisions share one key scheme. Opens navigate through the
 * same internal-route whitelist as local notifications.</p>
 */
public final class PushReceiver extends JPushMessageReceiver {
    private static final String TAG = "SpeakEasyPush";

    private static String eventId(Context context, NotificationMessage message) {
        try {
            String id = new JSONObject(message.notificationExtras).optString("event_id", "");
            if (!id.isEmpty() && id.length() <= 512) return id;
        } catch (Exception ignored) { }
        return message.msgId == null ? "" : message.msgId;
    }

    /**
     * Called by JPush before it displays a notification. When the app is in the
     * foreground the in-app banner is the single surface, so the system
     * notification is suppressed; read events never reappear.
     */
    @Override public boolean isNeedShowNotification(Context context, NotificationMessage message, String content) {
        String id = eventId(context, message);
        if (id.isEmpty()) return true;
        if (MainActivity.isForeground()) return false;
        return PushEvents.claimDisplay(context, id, message.notificationId);
    }

    @Override public void onNotifyMessageArrived(Context context, NotificationMessage message) {
        String id = eventId(context, message);
        if (id.isEmpty()) return;
        try {
            int previous = PushEvents.arrivedId(context, id);
            if (PushEvents.isRead(context, id) || (previous != -1 && previous != message.notificationId)) {
                JPushInterface.clearNotificationById(context, message.notificationId);
                return;
            }
            PushEvents.markArrived(context, id, message.notificationId);
        } catch (Throwable failure) {
            Log.w(TAG, "arrived handling failed: " + failure);
        }
    }

    @Override public void onRegister(Context context, String registrationId) {
        PushCoordinator.registered(context, registrationId);
    }

    @Override public void onNotifyMessageOpened(Context context, NotificationMessage message) {
        try {
            JSONObject extra = new JSONObject(message.notificationExtras);
            String id = extra.optString("event_id", message.msgId == null ? "" : message.msgId);
            int notificationId = PushEvents.isRead(context, id) ? -1 : PushEvents.markRead(context, id);
            if (notificationId > 0) JPushInterface.clearNotificationById(context, notificationId);
            if (message.notificationId > 0) JPushInterface.clearNotificationById(context, message.notificationId);
            if (!id.isEmpty()) LocalNotifications.cancel(context, id);
            String route = NativePolicy.routeOr(extra.optString("route", ""), "#/chats");
            Intent open = new Intent(context, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP)
                    .putExtra("route", route)
                    .putExtra("event_id", id);
            context.startActivity(open);
        } catch (Exception failure) {
            Log.w(TAG, "open handling failed: " + failure);
        }
    }

    /** Clears any JPush notification for an event id and marks it read. */
    static void cancel(Context context, String id) {
        if (id == null || id.isEmpty() || id.length() > 512) return;
        try {
            int notification = PushEvents.markRead(context, id);
            if (notification > 0) JPushInterface.clearNotificationById(context, notification);
            if (NotificationManagerCompat.from(context).areNotificationsEnabled()) LocalNotifications.cancel(context, id);
        } catch (Throwable failure) {
            Log.w(TAG, "cancel failed: " + failure);
        }
    }
}
