package com.speakeasy.app;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Map;

/**
 * Single owner of the push-event deduplication key scheme.
 *
 * <p>Keys are prefixed so the same event id can independently record that it was
 * seen by the SDK, arrived, was displayed and was read. {@link PushReceiver} and
 * {@link LocalNotifications} must both go through this class; a bare id key would
 * silently split the state and re-show read events.</p>
 */
final class PushEvents {
    private static final String PREFS = "push_events";
    private static final int MAX_ENTRIES = 512;

    private PushEvents() { }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static synchronized boolean isSeen(Context context, String id) {
        return !id.isEmpty() && prefs(context).getBoolean("seen:" + id, false);
    }

    static synchronized boolean isRead(Context context, String id) {
        return !id.isEmpty() && prefs(context).getBoolean("read:" + id, false);
    }

    static synchronized boolean isDelivered(Context context, String id) {
        return !id.isEmpty() && prefs(context).getInt("notification:" + id, -1) > 0;
    }

    static synchronized int notificationId(Context context, String id) {
        if (id.isEmpty()) return -1;
        return prefs(context).getInt("notification:" + id, -1);
    }

    /** Marks that the SDK is about to display this event; returns false when it must not. */
    static synchronized boolean claimDisplay(Context context, String id, int notificationId) {
        if (id.isEmpty()) return true;
        SharedPreferences preferences = prefs(context);
        if (preferences.getBoolean("read:" + id, false)) return false;
        if (preferences.getBoolean("seen:" + id, false)) return false;
        trim(preferences);
        preferences.edit().putBoolean("seen:" + id, true).putInt("notification:" + id, notificationId).apply();
        return true;
    }

    static synchronized void markArrived(Context context, String id, int notificationId) {
        if (id.isEmpty()) return;
        prefs(context).edit().putInt("arrived:" + id, notificationId).apply();
    }

    static synchronized int arrivedId(Context context, String id) {
        if (id.isEmpty()) return -1;
        return prefs(context).getInt("arrived:" + id, -1);
    }

    /** Marks an event read and returns the notification id that should be cleared, or -1. */
    static synchronized int markRead(Context context, String id) {
        if (id.isEmpty()) return -1;
        SharedPreferences preferences = prefs(context);
        int notification = preferences.getInt("notification:" + id, -1);
        preferences.edit().putBoolean("read:" + id, true).putBoolean("seen:" + id, true).apply();
        return notification;
    }

    /**
     * Releases a locally-claimed display slot after the local notification could
     * not be posted, so JPush may still show it.
     */
    static synchronized void releaseLocalClaim(Context context, String id) {
        if (id.isEmpty()) return;
        SharedPreferences preferences = prefs(context);
        if (preferences.getInt("notification:" + id, -1) != 0) return;
        preferences.edit().remove("seen:" + id).remove("notification:" + id).apply();
    }

    private static void trim(SharedPreferences preferences) {
        Map<String, ?> values = preferences.getAll();
        if (values.size() <= MAX_ENTRIES) return;
        preferences.edit().clear().apply();
    }
}
