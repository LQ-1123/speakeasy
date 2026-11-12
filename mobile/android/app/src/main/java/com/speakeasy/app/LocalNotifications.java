package com.speakeasy.app;

import android.Manifest;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONObject;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/**
 * Local system notifications for the WebView shell.
 *
 * <p>These are rendered by this app, not by JPush. They cover two situations the
 * push service cannot: (1) the installed build has no JPush credentials, and
 * (2) the page is hidden while the WebSocket still receives an event. Content is
 * deliberately generic — message text never leaves the page for the lock screen.</p>
 */
final class LocalNotifications {
    static final int PERMISSION_REQUEST = 44;

    static final String CHANNEL_MESSAGES = "speakeasy_messages";
    static final String CHANNEL_SOCIAL = "speakeasy_social";
    static final String CHANNEL_MESSAGES_SILENT = "speakeasy_messages_silent";
    static final String CHANNEL_SOCIAL_SILENT = "speakeasy_social_silent";

    private static final String PREFS = "speakeasy_notify";
    private static final Set<String> GENERIC_BODIES = new HashSet<>(Arrays.asList(
            "发来一条消息", "发来一条新消息", "有新的互动", "赞了你的动态", "评论了你的动态", "想成为你的语伴"));

    private LocalNotifications() { }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Creates the four channels once per process; existing channels keep their user settings. */
    static void ensureChannels(Context context) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        AudioAttributes attributes = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();
        String[][] definitions = {
                {CHANNEL_MESSAGES, "消息", "notify_message"},
                {CHANNEL_SOCIAL, "互动", "notify_request"},
                {CHANNEL_MESSAGES_SILENT, "消息（静音）", ""},
                {CHANNEL_SOCIAL_SILENT, "互动（静音）", ""},
        };
        for (String[] definition : definitions) {
            NotificationChannel channel = new NotificationChannel(definition[0], definition[1], NotificationManager.IMPORTANCE_DEFAULT);
            channel.setDescription("Sonder 语伴提醒");
            channel.setSound(definition[2].isEmpty() ? null
                    : Uri.parse("android.resource://" + context.getPackageName() + "/raw/" + definition[2]), attributes);
            channel.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
            if (definition[2].isEmpty()) channel.setShowBadge(false);
            manager.createNotificationChannel(channel);
        }
    }

    /**
     * Real capability/status payload consumed by the settings UI. Values are
     * observed from the OS, never assumed from the SDK version.
     */
    static String status(Context context) {
        boolean granted = Build.VERSION.SDK_INT < 33
                || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        boolean enabled = NotificationManagerCompat.from(context).areNotificationsEnabled();
        boolean channelReady = Build.VERSION.SDK_INT < 26;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager manager = context.getSystemService(NotificationManager.class);
            channelReady = manager != null && manager.getNotificationChannel(CHANNEL_MESSAGES) != null;
        }
        SharedPreferences preferences = prefs(context);
        SharedPreferences registrationPrefs = context.getSharedPreferences("push_registration", Context.MODE_PRIVATE);
        String registration = PushCoordinator.registrationId(context);
        Json json = new Json()
                .put("permission", granted ? "granted" : "denied")
                .put("enabled", enabled)
                .put("channelReady", channelReady)
                .put("push", PushCoordinator.available())
                .put("pushEnabled", PushCoordinator.pushEnabled(context))
                .put("pushState", PushCoordinator.state(context))
                .put("registrationId", registration == null || registration.isEmpty() ? JSONObject.NULL : registration)
                /* Last observed push result, so Settings can show real errors. */
                .put("lastPushStatus", registrationPrefs.getString("last_status", ""))
                .put("lastPushMessage", registrationPrefs.getString("last_message", ""))
                .put("lastPushAt", registrationPrefs.getLong("last_at", 0L))
                /* Diagnostics for the "registration timed out" case: distinguishes a
                   dead push connection from a connected-but-not-registered SDK. */
                .put("jpushConnected", PushCoordinator.connected(context))
                .put("pushStopped", PushCoordinator.stopped(context))
                .put("vendor", Build.MANUFACTURER)
                .put("networkResult", registrationPrefs.getString("net_result", ""))
                .put("networkResultAt", registrationPrefs.getLong("net_result_at", 0L))
                .put("sound", preferences.getBoolean("sound", true))
                .put("messages", preferences.getBoolean("messages", true))
                .put("interactions", preferences.getBoolean("interactions", true));
        return json.toString();
    }

    /** Requests POST_NOTIFICATIONS (33+); returns false when the OS cannot show a prompt. */
    static boolean requestPermission(Activity activity) {
        if (Build.VERSION.SDK_INT < 33) return NotificationManagerCompat.from(activity).areNotificationsEnabled();
        if (ContextCompat.checkSelfPermission(activity, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            return NotificationManagerCompat.from(activity).areNotificationsEnabled();
        }
        activity.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, PERMISSION_REQUEST);
        return true;
    }

    /** Kept as an explicit action: a denied permission can only be fixed in settings. */
    static void openSettings(Context context) {
        try {
            context.startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, context.getPackageName()));
        } catch (Exception first) {
            try {
                context.startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + context.getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (Exception ignored) { }
        }
    }

    /**
     * Posts one generic local notification. Returns true only when the OS accepted
     * the notification; never throws back into the WebView bridge.
     */
    static boolean post(Context context, String payloadJson) {
        try {
            JSONObject payload = new JSONObject(payloadJson);
            JSONObject session = new NativeIdentityStore(context).readSession();
            if (session == null) return false;
            String id = payload.optString("id", "").trim();
            if (id.isEmpty() || id.length() > 512) return false;
            String route = NativePolicy.routeOr(payload.optString("route", ""), "#/chats");
            boolean message = route.startsWith("#/chat");
            SharedPreferences preferences = prefs(context);
            if (message ? !preferences.getBoolean("messages", true) : !preferences.getBoolean("interactions", true)) return false;
            if (PushEvents.isRead(context, id) || PushEvents.isDelivered(context, id)) return false;
            if (!notificationsAllowed(context)) return false;
            ensureChannels(context);
            /* Body is whitelisted to generic actions: raw message text must never
               reach the lock screen, even if a caller passes it by mistake. */
            String title = clean(payload.optString("title", ""), 64);
            if (title.isEmpty()) title = "SpeakEasy";
            String rawBody = clean(payload.optString("body", ""), 32);
            String body = GENERIC_BODIES.contains(rawBody) ? rawBody : (message ? "发来一条消息" : "有新的互动");
            /* Claim the event before posting so a concurrently arriving JPush
               notification for the same event id is suppressed instead of showing twice. */
            if (!PushEvents.claimDisplay(context, id, 0)) return false;
            try {
                postClaimed(context, id, route, title, body, session.getString("userId"));
                return true;
            } catch (Throwable failure) {
                PushEvents.releaseLocalClaim(context, id);
                return false;
            }
        } catch (Exception | LinkageError ignored) {
            return false;
        }
    }

    private static void postClaimed(Context context, String id, String route, String title, String body, String userId) {
        boolean message = route.startsWith("#/chat");
        boolean sound = prefs(context).getBoolean("sound", true);
        String channel = (message ? CHANNEL_MESSAGES : CHANNEL_SOCIAL) + (sound ? "" : "_silent");
        int notificationId = notificationId(id);

        Intent open = new Intent(context, MainActivity.class)
                .setAction("com.speakeasy.app.NOTIFICATION_OPEN")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP)
                .putExtra("route", route)
                .putExtra("event_id", id)
                .putExtra("user_id", userId);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent pending = PendingIntent.getActivity(context, notificationId, open, flags);

        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, channel)
                /* 状态栏小图标必须是单色透明剪影：用 Sonder 的 S，而不是渐变桌面图标。 */
                .setSmallIcon(R.drawable.jpush_notification_icon)
                .setLargeIcon(android.graphics.BitmapFactory.decodeResource(
                        context.getResources(), R.mipmap.ic_launcher_dynamic_background))
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setCategory(message ? NotificationCompat.CATEGORY_MESSAGE : NotificationCompat.CATEGORY_SOCIAL)
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setContentIntent(pending)
                .setAutoCancel(true)
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true);
        if (Build.VERSION.SDK_INT < 26) {
            if (sound) builder.setDefaults(NotificationCompat.DEFAULT_ALL);
            else builder.setSilent(true);
        }
        NotificationManagerCompat.from(context).notify(notificationId, builder.build());
    }

    /** Cancels the local notification for an event; safe to call repeatedly. */
    static void cancel(Context context, String eventId) {
        if (eventId == null || eventId.isEmpty() || eventId.length() > 512) return;
        try {
            NotificationManagerCompat.from(context).cancel(notificationId(eventId));
            PushEvents.markRead(context, eventId);
        } catch (Exception ignored) { }
    }

    private static boolean notificationsAllowed(Context context) {
        if (Build.VERSION.SDK_INT >= 33
                && ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return false;
        }
        return NotificationManagerCompat.from(context).areNotificationsEnabled();
    }

    private static int notificationId(String eventId) {
        int id = eventId.hashCode() & 0x7fffffff;
        return id == 0 ? 1 : id;
    }

    private static String clean(String value, int maxLength) {
        String text = value == null ? "" : value.replaceAll("[\\u0000-\\u001f\\u007f]", " ").trim();
        return text.length() <= maxLength ? text : text.substring(0, maxLength);
    }
}
