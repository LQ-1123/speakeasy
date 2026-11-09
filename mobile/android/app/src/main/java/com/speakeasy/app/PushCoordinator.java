package com.speakeasy.app;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;
import cn.jpush.android.api.JPushInterface;
import android.net.Uri;
import android.media.AudioAttributes;

final class PushCoordinator {
    static boolean available() { return BuildConfig.JPUSH_APP_KEY.matches("[a-zA-Z0-9]{24}"); }
    static void request(Activity activity) {
        if (!available()) { event(activity, "unavailable", "JPush app key is not configured", null); return; }
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(activity, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            activity.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 42); return;
        }
        permissionResult(activity, NotificationManagerCompat.from(activity).areNotificationsEnabled());
    }
    static void permissionResult(Context context, boolean granted) {
        if (!granted) { event(context, "denied", "Enable notifications in Android settings", null); return; }
        if (!available()) { event(context, "unavailable", "JPush app key is not configured", null); return; }
        try {
            channels(context);
            JPushInterface.setDebugMode(false);
            JPushInterface.init(context.getApplicationContext());
            JPushInterface.resumePush(context.getApplicationContext());
            String registration = JPushInterface.getRegistrationID(context);
            if (registration == null || registration.isEmpty()) event(context, "registering", "Waiting for push registration", null);
            else event(context, "registered", "", registration);
        } catch (Exception failure) { event(context, "error", failure.getMessage(), null); }
    }
    static void preferences(Activity activity, boolean enabled) {
        activity.getPreferences(Context.MODE_PRIVATE).edit().putBoolean("pushEnabled", enabled).apply();
        if (!available()) return;
        if (!enabled) { JPushInterface.stopPush(activity.getApplicationContext()); return; }
        if ((Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(activity, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
                && NotificationManagerCompat.from(activity).areNotificationsEnabled()) permissionResult(activity, true);
    }
    static void resume(Activity activity) {
        if (activity.getPreferences(Context.MODE_PRIVATE).getBoolean("pushEnabled", true)) preferences(activity, true);
    }
    private static void channels(Context context) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        AudioAttributes attributes = new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build();
        String[][] definitions = {{"speakeasy_messages", "Messages", "notify_message"}, {"speakeasy_social", "Interactions", "notify_request"}, {"speakeasy_messages_silent", "Messages (silent)", ""}, {"speakeasy_social_silent", "Interactions (silent)", ""}};
        for (String[] definition : definitions) {
            NotificationChannel channel = new NotificationChannel(definition[0], definition[1], NotificationManager.IMPORTANCE_DEFAULT);
            channel.setSound(definition[2].isEmpty() ? null : Uri.parse("android.resource://" + context.getPackageName() + "/raw/" + definition[2]), attributes);
            channel.setLockscreenVisibility(android.app.Notification.VISIBILITY_PRIVATE);
            manager.createNotificationChannel(channel);
        }
    }
    static void event(Context context, String status, String message, String registration) {
        Json detail = new Json().put("status", status).put("message", message).put("vendor", Build.MANUFACTURER);
        if (registration != null && !registration.isEmpty()) detail.put("registration_id", registration);
        context.getSharedPreferences("push_registration", Context.MODE_PRIVATE).edit().putString("pending", detail.toString()).apply();
        MainActivity.dispatchNativeEvent(context, "push", detail);
    }
    static void deliverPending(Context context) {
        String value = context.getSharedPreferences("push_registration", Context.MODE_PRIVATE).getString("pending", null);
        if (value == null) return;
        try { MainActivity.dispatchNativeEvent(context, "push", new org.json.JSONObject(value)); }
        catch (org.json.JSONException ignored) { }
    }
}
