package com.speakeasy.app;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;

import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import cn.jiguang.api.JCoreInterface;
import cn.jiguang.api.utils.JCollectionAuth;
import cn.jpush.android.api.JPushInterface;

import org.json.JSONObject;

import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Owns the JPush lifecycle: channels, SDK init, registration polling and the
 * single "registered" event the frontend needs to bind this device to an account.
 *
 * <p>Registration id lookup is asynchronous inside JPush: reading it immediately
 * after {@code init} usually returns "" and there is no reliable callback in all
 * network conditions. We therefore poll for up to ~10s and emit at most one
 * registered event per distinct registration id.</p>
 */
final class PushCoordinator {
    private static final String TAG = "SpeakEasyPush";
    private static final String REGISTRATION_PREFS = "push_registration";
    private static final String NOTIFY_PREFS = "speakeasy_notify";
    private static final int POLL_ATTEMPTS = 20;
    private static final long POLL_INTERVAL_MS = 500L;
    private static final long POLL_COOLDOWN_MS = 30_000L;

    private static final Object POLL_LOCK = new Object();
    private static boolean polling;
    private static volatile boolean userWaitingForPoll;
    private static long lastPollAt;

    private PushCoordinator() { }

    static boolean available() {
        return BuildConfig.JPUSH_APP_KEY != null && BuildConfig.JPUSH_APP_KEY.matches("[a-zA-Z0-9]{24}");
    }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(NOTIFY_PREFS, Context.MODE_PRIVATE);
    }

    static boolean pushEnabled(Context context) {
        return prefs(context).getBoolean("pushEnabled", true);
    }

    static void setPushEnabled(Context context, boolean enabled) {
        prefs(context).edit().putBoolean("pushEnabled", enabled).apply();
    }

    /**
     * JCore refuses to register/collect until {@code JCollectionAuth.setAuth(true)}
     * was called after the user consented. The explicit preference is written when
     * the user enables notifications; an existing OS notification grant also counts
     * (the user already said yes), so upgrading users do not have to reopen Settings.
     */
    static boolean consented(Context context) {
        if (prefs(context).getBoolean("consent", false)) return true;
        boolean granted = Build.VERSION.SDK_INT < 33
                || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        return granted && NotificationManagerCompat.from(context).areNotificationsEnabled();
    }

    static void setConsented(Context context) {
        prefs(context).edit().putBoolean("consent", true).apply();
    }

    /** Called from {@link SpeakEasyApp#onCreate()} — must stay cheap and never throw. */
    static void initialize(Context context) {
        try {
            LocalNotifications.ensureChannels(context);
        } catch (Throwable failure) {
            Log.w(TAG, "notification channels unavailable: " + failure);
        }
        if (!available()) return;
        if (!consented(context)) {
            /* Before consent JCore must not initialise or collect anything. */
            try {
                JCollectionAuth.setAuth(context.getApplicationContext(), false);
            } catch (Throwable ignored) { }
            return;
        }
        try {
            JCollectionAuth.setAuth(context.getApplicationContext(), true);
            JPushInterface.setDebugMode(false);
            JPushInterface.init(context.getApplicationContext());
            if (pushEnabled(context)) {
                JPushInterface.resumePush(context.getApplicationContext());
                beginRegistration(context, false);
            } else {
                JPushInterface.stopPush(context.getApplicationContext());
            }
        } catch (Throwable failure) {
            Log.w(TAG, "JPush init failed: " + failure);
        }
    }

    /** User-initiated "enable notifications" flow from the settings card. */
    static void request(Activity activity) {
        if (!available()) {
            event(activity, "unavailable", "当前安装包未配置推送服务（缺少 JPush AppKey）", null);
            return;
        }
        if (Build.VERSION.SDK_INT >= 33
                && ContextCompat.checkSelfPermission(activity, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            activity.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 42);
            return;
        }
        permissionResult(activity, NotificationManagerCompat.from(activity).areNotificationsEnabled());
    }

    static void permissionResult(Context context, boolean granted) {
        if (!available()) {
            event(context, "unavailable", "当前安装包未配置推送服务（缺少 JPush AppKey）", null);
            return;
        }
        if (!granted) {
            event(context, "denied", "通知权限未开启，请在系统设置中允许 SpeakEasy 发送通知", null);
            return;
        }
        /* User just consented: this is what unlocks JCore registration. */
        setConsented(context);
        setPushEnabled(context, true);
        try {
            LocalNotifications.ensureChannels(context);
            JCollectionAuth.setAuth(context.getApplicationContext(), true);
            JPushInterface.setDebugMode(false);
            JPushInterface.init(context.getApplicationContext());
            JPushInterface.resumePush(context.getApplicationContext());
        } catch (Throwable failure) {
            event(context, "error", "推送初始化失败，请稍后重试", null);
            return;
        }
        beginRegistration(context, true);
    }

    static void preferences(Activity activity, boolean enabled) {
        setPushEnabled(activity, enabled);
        if (!available()) return;
        if (!enabled) {
            try {
                JPushInterface.stopPush(activity.getApplicationContext());
            } catch (Throwable failure) {
                Log.w(TAG, "stopPush failed: " + failure);
            }
            return;
        }
        if ((Build.VERSION.SDK_INT < 33
                || ContextCompat.checkSelfPermission(activity, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
                && NotificationManagerCompat.from(activity).areNotificationsEnabled()) {
            permissionResult(activity, true);
        }
    }

    /** Called after the page is ready and on resume; idempotent and rate limited. */
    static void resume(Context context) {
        if (!available()) return;
        if (!consented(context)) return;
        if (!pushEnabled(context)) {
            try {
                JPushInterface.stopPush(context.getApplicationContext());
            } catch (Throwable ignored) { }
            return;
        }
        try {
            JCollectionAuth.setAuth(context.getApplicationContext(), true);
            JPushInterface.init(context.getApplicationContext());
            JPushInterface.resumePush(context.getApplicationContext());
        } catch (Throwable failure) {
            Log.w(TAG, "JPush resume failed: " + failure);
            return;
        }
        beginRegistration(context, false);
    }

    /**
     * Polls JPush for the registration id in the background. Single flight and
     * cooldown prevent a storm of polls across activity resumes.
     */
    static void beginRegistration(Context context, boolean reportWaiting) {
        if (!available()) {
            if (reportWaiting) event(context, "unavailable", "当前安装包未配置推送服务（缺少 JPush AppKey）", null);
            return;
        }
        final Context app = context.getApplicationContext();
        String current = registrationId(app);
        if (current != null && !current.isEmpty()) {
            registered(app, current);
            return;
        }
        final int attempts = reportWaiting ? POLL_ATTEMPTS * 2 : POLL_ATTEMPTS;
        synchronized (POLL_LOCK) {
            long now = System.currentTimeMillis();
            /* Passive resume polls are rate limited; an explicit user tap must never
               be swallowed by that cooldown, otherwise the settings card sits on
               "正在注册推送" forever after one timeout. */
            if (!reportWaiting && (polling || now - lastPollAt < POLL_COOLDOWN_MS)) return;
            if (polling && reportWaiting) {
                /* A passive poll is already running; let its result stand, but tell
                   the page that a registration attempt is in progress. The poll then
                   reports timeout failures to the user instead of staying silent. */
                userWaitingForPoll = true;
                event(app, "registering", "正在等待推送注册…", null);
                return;
            }
            polling = true;
            lastPollAt = now;
        }
        if (reportWaiting) event(app, "registering", "正在等待推送注册…", null);
        Thread worker = new Thread(() -> {
            boolean delivered = false;
            try {
                for (int attempt = 0; attempt < attempts; attempt++) {
                    if (!pushEnabled(app)) return;
                    String id = JPushInterface.getRegistrationID(app);
                    if (id != null && !id.isEmpty()) {
                        registered(app, id);
                        delivered = true;
                        return;
                    }
                    Thread.sleep(POLL_INTERVAL_MS);
                }
                if (!delivered && (reportWaiting || userWaitingForPoll)) event(app, "error", "推送注册超时，请检查网络后重试", null);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            } catch (Throwable failure) {
                Log.w(TAG, "registration poll failed: " + failure);
                if (reportWaiting) event(app, "error", "推送注册失败，请稍后重试", null);
            } finally {
                synchronized (POLL_LOCK) { polling = false; userWaitingForPoll = false; }
            }
        }, "speakeasy-jpush-register");
        worker.setDaemon(true);
        worker.start();
    }

    /**
     * Emits exactly one registered event per registration id. Repeated callbacks and
     * resumes with the same id stay silent.
     */
    static void registered(Context context, String registrationId) {
        if (registrationId == null || registrationId.isEmpty()) return;
        SharedPreferences preferences = context.getSharedPreferences(REGISTRATION_PREFS, Context.MODE_PRIVATE);
        String delivered = preferences.getString("delivered_id", "");
        preferences.edit().putString("registration_id", registrationId).apply();
        Log.i(TAG, "registered id=" + registrationId.substring(0, Math.min(8, registrationId.length())) + "…");
        if (registrationId.equals(delivered)) return;
        preferences.edit().putString("delivered_id", registrationId).apply();
        event(context, "registered", "", registrationId);
    }

    static String registrationId(Context context) {
        if (!available()) return "";
        try {
            String id = JPushInterface.getRegistrationID(context);
            if (id != null && !id.isEmpty()) return id;
        } catch (Throwable failure) {
            Log.w(TAG, "getRegistrationID failed: " + failure);
        }
        return context.getSharedPreferences(REGISTRATION_PREFS, Context.MODE_PRIVATE).getString("registration_id", "");
    }

    static String state(Context context) {
        if (!available()) return "unconfigured";
        if (!pushEnabled(context)) return "disabled";
        String id = registrationId(context);
        return id == null || id.isEmpty() ? "idle" : "registered";
    }

    /**
     * Whether the JPush long connection is up. False right after init/network loss
     * is normal, but a persistently false value means the phone cannot reach the
     * JPush servers (the settings card shows that as the timeout reason).
     */
    static boolean connected(Context context) {
        if (!available()) return false;
        try {
            return JCoreInterface.getConnectionState(context.getApplicationContext());
        } catch (Throwable failure) {
            return false;
        }
    }

    static boolean stopped(Context context) {
        if (!available()) return false;
        try {
            return JPushInterface.isPushStopped(context.getApplicationContext());
        } catch (Throwable failure) {
            return false;
        }
    }

    /**
     * User-triggered connectivity probe for "registration timed out". Resolves and
     * TCP-connects to the JPush hosts off the UI thread, stores the result and emits
     * one "network" push event so the settings card can show it.
     */
    static void probeNetwork(final Context context) {
        final Context app = context.getApplicationContext();
        Thread worker = new Thread(() -> {
            StringBuilder report = new StringBuilder();
            /* Official JPush client-side hosts (community article 467502): the long
               connection is not sdk.jpush.cn (that name has no public DNS anymore). */
            String[][] targets = {
                    {"s.jpush.cn", "7000"},
                    {"s.jpush.cn", "443"},
                    {"im64.jpush.cn", "7000"},
                    {"im64.jpush.cn", "443"},
                    {"stats.jpush.cn", "443"},
                    {"bjuser.jpush.cn", "443"},
                    {"api.jpush.cn", "443"},
            };
            for (String[] target : targets) {
                String state = "FAIL";
                long started = System.currentTimeMillis();
                try {
                    InetAddress[] addresses = InetAddress.getAllByName(target[0]);
                    if (addresses == null || addresses.length == 0) throw new IOException("DNS 无结果");
                    int port = Integer.parseInt(target[1]);
                    Exception lastError = null;
                    int tried = 0;
                    for (InetAddress address : addresses) {
                        if (tried++ >= 3) break;
                        Socket socket = new Socket();
                        try {
                            socket.connect(new InetSocketAddress(address, port), 2500);
                            state = "OK " + address.getHostAddress();
                            lastError = null;
                            break;
                        } catch (Exception failure) {
                            lastError = failure;
                        } finally {
                            try { socket.close(); } catch (IOException ignored) { }
                        }
                    }
                    if (lastError != null) throw lastError;
                } catch (Throwable failure) {
                    String message = failure.getMessage();
                    state = "FAIL " + failure.getClass().getSimpleName() + (message == null ? "" : (": " + message));
                }
                if (report.length() > 0) report.append('\n');
                report.append(target[0]).append(':').append(target[1]).append(" → ")
                        .append(state).append(" (").append(System.currentTimeMillis() - started).append("ms)");
            }
            report.append("\n极光长连接：").append(connected(app) ? "已连接" : "未连接");
            String result = report.toString();
            prefs(app).edit().putString("net_result", result)
                    .putLong("net_result_at", System.currentTimeMillis()).apply();
            event(app, "network", result, null);
        }, "speakeasy-push-net");
        worker.setDaemon(true);
        worker.start();
    }

    /**
     * Persists the event and delivers it to the page once. Delivery is gated on the
     * WebView being ready; each event carries an {@code at} timestamp so a stored
     * event is not replayed on every launch.
     */
    static void event(Context context, String status, String message, String registration) {
        Json detail = new Json().put("status", status).put("message", message == null ? "" : message)
                .put("vendor", Build.MANUFACTURER).put("at", System.currentTimeMillis());
        if (registration != null && !registration.isEmpty()) detail.put("registration_id", registration);
        try {
            context.getSharedPreferences(REGISTRATION_PREFS, Context.MODE_PRIVATE).edit()
                    .putString("pending", detail.toString())
                    /* Last observed result: the settings card shows real failures even
                       after the transient toast is gone. */
                    .putString("last_status", status)
                    .putString("last_message", message == null ? "" : message)
                    .putLong("last_at", detail.optLong("at", 0L))
                    .apply();
        } catch (Throwable ignored) { }
        deliverPending(context);
    }

    static void deliverPending(Context context) {
        if (!MainActivity.isPageReady()) return;
        try {
            SharedPreferences preferences = context.getSharedPreferences(REGISTRATION_PREFS, Context.MODE_PRIVATE);
            String value = preferences.getString("pending", null);
            if (value == null) return;
            JSONObject detail = new JSONObject(value);
            long at = detail.optLong("at", 0L);
            if (at > 0L && at <= preferences.getLong("delivered_at", 0L)) return;
            if (!MainActivity.dispatchNativeEvent(context, "push", detail)) return;
            if (at > 0L) preferences.edit().putLong("delivered_at", at).apply();
        } catch (Exception ignored) { }
    }
}
