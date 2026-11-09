package com.speakeasy.app;

import android.content.Context;
import android.content.Intent;
import cn.jpush.android.api.NotificationMessage;
import cn.jpush.android.service.JPushMessageReceiver;
import org.json.JSONObject;

public final class PushReceiver extends JPushMessageReceiver {
    private static String eventId(NotificationMessage message) {
        try { return new JSONObject(message.notificationExtras).optString("event_id", message.msgId == null ? "" : message.msgId); }
        catch (Exception failure) { return message.msgId == null ? "" : message.msgId; }
    }
    @Override public boolean isNeedShowNotification(Context context, NotificationMessage message, String content) {
        String id = eventId(message);
        if (id.isEmpty()) return true;
        android.content.SharedPreferences events = context.getSharedPreferences("push_events", Context.MODE_PRIVATE);
        synchronized (PushReceiver.class) {
            if (events.getBoolean("seen:" + id, false) || events.getBoolean("read:" + id, false)) return false;
            if (events.getAll().size() > 1024) events.edit().clear().apply();
            events.edit().putBoolean("seen:" + id, true).putInt("notification:" + id, message.notificationId).apply();
        }
        return true;
    }
    @Override public void onNotifyMessageArrived(Context context, NotificationMessage message) {
        String id = eventId(message);
        if (id.isEmpty()) return;
        android.content.SharedPreferences events = context.getSharedPreferences("push_events", Context.MODE_PRIVATE);
        synchronized (PushReceiver.class) {
            int previous = events.getInt("arrived:" + id, -1);
            if (events.getBoolean("read:" + id, false) || (previous != -1 && previous != message.notificationId)) {
                cn.jpush.android.api.JPushInterface.clearNotificationById(context, message.notificationId); return;
            }
            events.edit().putInt("arrived:" + id, message.notificationId).putInt("notification:" + id, message.notificationId).apply();
        }
    }
    static void cancel(Context context, String id) {
        if (id == null || id.isEmpty() || id.length() > 512) return;
        android.content.SharedPreferences events = context.getSharedPreferences("push_events", Context.MODE_PRIVATE);
        int notification = events.getInt("notification:" + id, -1);
        events.edit().putBoolean("read:" + id, true).apply();
        if (notification != -1) cn.jpush.android.api.JPushInterface.clearNotificationById(context, notification);
    }
    @Override public void onRegister(Context context, String registrationId) {
        PushCoordinator.event(context, "registered", "", registrationId);
    }
    @Override public void onNotifyMessageOpened(Context context, NotificationMessage message) {
        try {
            JSONObject extra = new JSONObject(message.notificationExtras);
            String id = extra.optString("event_id", message.msgId);
            android.content.SharedPreferences seen = context.getSharedPreferences("push_events", Context.MODE_PRIVATE);
            if (!id.isEmpty() && seen.getBoolean(id, false)) return;
            if (!id.isEmpty()) {
                if (seen.getAll().size() > 256) seen.edit().clear().apply();
                seen.edit().putBoolean(id, true).apply();
            }
            String route = extra.optString("route", "#/messages");
            if (!route.startsWith("#/") || route.length() > 512) route = "#/messages";
            Intent open = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP)
                    .putExtra("route", route).putExtra("event_id", id);
            context.startActivity(open);
        } catch (Exception ignored) { }
    }
}
