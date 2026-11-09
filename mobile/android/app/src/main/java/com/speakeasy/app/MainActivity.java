package com.speakeasy.app;

import android.app.Activity;
import android.Manifest;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.media.MediaPlayer;
import android.app.UiModeManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Looper;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import androidx.activity.OnBackPressedCallback;
import androidx.core.content.ContextCompat;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebViewClient;
import org.json.JSONObject;
import java.util.Iterator;

public class MainActivity extends BridgeActivity {
    private static volatile MainActivity active;
    private NativeIdentityStore identities;
    private volatile JSONObject callState = new Json().put("state", "idle");
    private String themePreference = "system";
    private WebView webView;
    private volatile String migration = "pending";
    private volatile String identityError = "";
    private String pendingCall;
    private String pendingRoute;
    private int routeAttempts;
    private int pushAttempts;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        active = this;
        try {
            identities = new NativeIdentityStore(this);
            new IdentityMigration(this, identities, (status, error) -> {
                migration = status; identityError = error;
                dispatch(new Json().put("type", "identity").put("status", status).put("message", error));
            }).run();
        } catch (Exception failure) { migration = "failed"; identityError = "Secure identity storage unavailable"; }
        themePreference = getPreferences(MODE_PRIVATE).getString("theme", "system");
        webView = getBridge() == null ? null : getBridge().getWebView();
        if (webView != null) {
            webView.getSettings().setMediaPlaybackRequiresUserGesture(false);
            webView.setBackgroundColor(Color.TRANSPARENT);
            webView.addJavascriptInterface(new NativeBridge(), "SpeakEasyNative");
            webView.setWebViewClient(new BridgeWebViewClient(getBridge()) {
                @Override public boolean shouldOverrideUrlLoading(WebView view, android.webkit.WebResourceRequest request) {
                    android.net.Uri url = request.getUrl();
                    if ("https".equals(url.getScheme()) && "localhost".equals(url.getHost()) && (url.getPort() == -1 || url.getPort() == 443)) return super.shouldOverrideUrlLoading(view, request);
                    return true;
                }
                @Override public void onPageFinished(WebView view, String url) {
                    super.onPageFinished(view, url);
                    if (url.startsWith("https://localhost")) {
                        handleNotificationIntent(getIntent());
                        dispatchNativeEvent(MainActivity.this, "call", CallService.snapshot());
                        schedulePushDelivery();
                    }
                }
                @Override public android.webkit.WebResourceResponse shouldInterceptRequest(WebView view, android.webkit.WebResourceRequest request) {
                    android.webkit.WebResourceResponse response = super.shouldInterceptRequest(view, request);
                    if (response != null && "localhost".equals(request.getUrl().getHost()) && "text/html".equals(response.getMimeType())) {
                        java.util.Map<String, String> headers = new java.util.HashMap<>();
                        if (response.getResponseHeaders() != null) headers.putAll(response.getResponseHeaders());
                        headers.put("Content-Security-Policy", "frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'; script-src 'self' 'unsafe-inline' blob:");
                        response.setResponseHeaders(headers);
                    }
                    return response;
                }
            });
            ViewCompat.setOnApplyWindowInsetsListener(webView, (view, insets) -> {
                int top = insets.getInsets(WindowInsetsCompat.Type.statusBars()).top;
                int bottom = insets.getInsets(WindowInsetsCompat.Type.navigationBars()).bottom;
                int ime = insets.getInsets(WindowInsetsCompat.Type.ime()).bottom;
                JSONObject detail = new Json().put("type", "theme").put("insets", new Json()
                        .put("top", dp(top)).put("bottom", dp(bottom)).put("ime", dp(ime)));
                dispatch(detail);
                return insets;
            });
        }
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        updateSystemBars();
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                WindowInsetsCompat insets = ViewCompat.getRootWindowInsets(getWindow().getDecorView());
                if (insets != null && insets.isVisible(WindowInsetsCompat.Type.ime())) {
                    WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
                    controller.hide(WindowInsetsCompat.Type.ime());
                    return;
                }
                WebView view = getBridge() == null ? null : getBridge().getWebView();
                if (view == null) { finish(); return; }
                view.evaluateJavascript("(function(){return typeof window.speakeasyBack === 'function' ? window.speakeasyBack() : false})()", value -> {
                    if (NativePolicy.backHandled(value)) return;
                    view.evaluateJavascript("(function(){if(history.length>1){history.back();return true}return false})()", history -> {
                        if (!"true".equals(history)) moveTaskToBack(true);
                    });
                });
            }
        });
        UpdateChecker.checkAndPrompt(this);
        // Interfaces added after BridgeActivity initializes must be present from document start.
        if (webView != null) webView.reload();
    }

    private int dp(int px) { return Math.round(px / getResources().getDisplayMetrics().density); }
    private boolean dark() {
        if ("dark".equals(themePreference)) return true;
        if ("light".equals(themePreference)) return false;
        return (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
    }
    private void updateSystemBars() {
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        boolean isDark = dark();
        controller.setAppearanceLightStatusBars(!isDark);
        controller.setAppearanceLightNavigationBars(!isDark);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
    }

    static void dispatchNativeEvent(Context context, String type, JSONObject value) {
        MainActivity activity = active;
        if (activity == null) return;
        activity.runOnUiThread(() -> {
            Json detail = new Json().put("type", "event".equals(type) ? "call" : type);
            if ("event".equals(type)) detail.put("event", value).put("state", CallService.snapshot());
            else if ("call".equals(type)) { activity.callState = value; detail.put("state", value); }
            else { Iterator<String> keys = value.keys(); while (keys.hasNext()) { String key = keys.next(); detail.put(key, value.opt(key)); } }
            activity.dispatch(detail);
        });
    }

    private void dispatch(JSONObject detail) {
        if (webView == null) return;
        if (Looper.myLooper() != Looper.getMainLooper()) { runOnUiThread(() -> dispatch(detail)); return; }
        String json = JSONObject.quote(detail.toString());
        webView.evaluateJavascript("window.dispatchEvent(new CustomEvent('speakeasy:native',{detail:JSON.parse(" + json + ")}))", null);
    }

    private void handleNotificationIntent(Intent intent) {
        String route = intent == null ? null : intent.getStringExtra("route");
        if (route != null && route.startsWith("#/") && route.length() < 512) {
            pendingRoute = route; routeAttempts = 0;
            getWindow().getDecorView().removeCallbacks(deliverRoute);
            getWindow().getDecorView().post(deliverRoute);
        }
    }
    private final Runnable deliverRoute = new Runnable() { @Override public void run() {
        if (webView == null || pendingRoute == null || routeAttempts++ > 120) return;
        webView.evaluateJavascript("typeof window.speakeasyBack === 'function'", ready -> {
            if ("true".equals(ready) && pendingRoute != null) {
                dispatch(new Json().put("type", "notification_open").put("route", pendingRoute));
                pendingRoute = null; getIntent().removeExtra("route");
            } else getWindow().getDecorView().postDelayed(this, 250);
        });
    }};
    private void schedulePushDelivery() { pushAttempts = 0; getWindow().getDecorView().removeCallbacks(deliverPush); getWindow().getDecorView().post(deliverPush); }
    private final Runnable deliverPush = new Runnable() { @Override public void run() {
        if (webView == null || pushAttempts++ > 120) return;
        webView.evaluateJavascript("typeof window.speakeasyBack === 'function'", ready -> {
            if ("true".equals(ready)) { PushCoordinator.resume(MainActivity.this); PushCoordinator.deliverPending(MainActivity.this); }
            else getWindow().getDecorView().postDelayed(this, 250);
        });
    }};
    @Override protected void onNewIntent(Intent intent) { super.onNewIntent(intent); setIntent(intent); handleNotificationIntent(intent); }
    @Override public void onConfigurationChanged(Configuration configuration) { super.onConfigurationChanged(configuration); updateSystemBars(); dispatch(new Json().put("type", "theme")); }
    @Override public void onResume() { super.onResume(); active = this; UpdateChecker.onResume(this); handleNotificationIntent(getIntent()); schedulePushDelivery(); }
    @Override public void onDestroy() { if (active == this) active = null; getWindow().getDecorView().removeCallbacks(deliverRoute); getWindow().getDecorView().removeCallbacks(deliverPush); super.onDestroy(); }

    public final class NativeBridge {
        @JavascriptInterface public String getCapabilities() {
            boolean pushConfigured = PushCoordinator.available();
            return new Json().put("version", 1).put("nativeCalls", true).put("dynamicColor", Build.VERSION.SDK_INT >= 31)
                    .put("push", pushConfigured).put("pushProvider", pushConfigured ? "jpush" : JSONObject.NULL)
                    .put("identityMigration", migration).put("identityError", identityError).toString();
        }
        @JavascriptInterface public String getTheme() {
            int accent = Build.VERSION.SDK_INT >= 31 ? getColor(dark() ? android.R.color.system_accent1_200 : android.R.color.system_accent1_600) : Color.rgb(52, 118, 93);
            Json inset = new Json();
            if (webView != null) {
                WindowInsetsCompat insets = ViewCompat.getRootWindowInsets(webView);
                if (insets != null) inset.put("top", dp(insets.getInsets(WindowInsetsCompat.Type.statusBars()).top))
                        .put("bottom", dp(insets.getInsets(WindowInsetsCompat.Type.navigationBars()).bottom))
                        .put("ime", dp(insets.getInsets(WindowInsetsCompat.Type.ime()).bottom));
            }
            return new Json().put("accent", String.format("#%06X", (0xFFFFFF & accent))).put("onAccent", dark() ? "#10251B" : "#FFFFFF")
                    .put("dark", dark()).put("insets", inset).toString();
        }
        @JavascriptInterface public String getIdentity() { try {
            if (identities == null || "pending".equals(migration) || "failed".equals(migration)) return "null";
            JSONObject identity = identities.read();
            if (identity == null && identities.deviceKey() == null) return "null";
            if (identity == null) identity = new Json();
            if (identities.deviceKey() != null) identity.put("device_key", identities.deviceKey());
            return identity.toString();
        } catch (Exception e) { return "null"; } }
        @JavascriptInterface public void saveIdentity(String json) { try { if (identities == null || "failed".equals(migration) || "pending".equals(migration)) throw new IllegalStateException("Secure identity migration incomplete"); JSONObject identity = new JSONObject(json); identities.save(identity); identities.saveDeviceKey(identity.optString("device_key", null)); migration = "ready"; dispatch(new Json().put("type", "identity").put("status", "saved")); } catch (Exception e) { dispatch(new Json().put("type", "identity").put("status", "error").put("message", e.getMessage())); } }
        @JavascriptInterface public void startCall(String json) {
            try {
                if (ContextCompat.checkSelfPermission(MainActivity.this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    pendingCall = json;
                    runOnUiThread(() -> requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 41));
                    return;
                }
                JSONObject call = new JSONObject(json);
                if (!call.optString("apiOrigin").equals(BuildConfig.API_BASE)) throw new SecurityException("Unconfigured API origin");
                if (!call.optString("wsPath").startsWith("/api/v3/calls/")) throw new SecurityException("Invalid call socket path");
                NativePolicy.socketUrl(call.getString("apiOrigin"), call.getString("wsPath"), call.getString("callId"));
                JSONObject identity = identities == null ? null : identities.read();
                if (identity == null || !identity.optString("user_id").equals(call.optString("userId")) || !identity.optString("token").equals(call.optString("token"))) throw new SecurityException("Call identity unavailable");
                if (Build.VERSION.SDK_INT >= 31 && ContextCompat.checkSelfPermission(MainActivity.this, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED && !getPreferences(MODE_PRIVATE).getBoolean("bluetoothAsked", false)) {
                    pendingCall = json; getPreferences(MODE_PRIVATE).edit().putBoolean("bluetoothAsked", true).apply();
                    runOnUiThread(() -> requestPermissions(new String[]{Manifest.permission.BLUETOOTH_CONNECT}, 43)); return;
                }
                Intent service = new Intent(MainActivity.this, CallService.class).setAction(CallService.ACTION_START).putExtra("json", json);
                ContextCompat.startForegroundService(MainActivity.this, service);
                callState = new Json().put("state", "connecting").put("callId", call.optString("callId"));
                dispatch(new Json().put("type", "call").put("state", callState));
            } catch (Exception e) { dispatch(new Json().put("type", "call").put("state", new Json().put("state", "error").put("message", e.getMessage()))); }
        }
        @JavascriptInterface public void callAction(String json) { if (!"idle".equals(CallService.snapshot().optString("state")) && !"ended".equals(CallService.snapshot().optString("state"))) startService(new Intent(MainActivity.this, CallService.class).setAction(CallService.ACTION_CONTROL).putExtra("json", json)); }
        @JavascriptInterface public String getCallState() { return CallService.snapshot().toString(); }
        @JavascriptInterface public void requestPush() { runOnUiThread(() -> PushCoordinator.request(MainActivity.this)); }
        @JavascriptInterface public void cancelNotification(String eventId) { PushReceiver.cancel(MainActivity.this, eventId); }
        @JavascriptInterface public void setPreferences(String json) { try { JSONObject value = new JSONObject(json); themePreference = value.optString("theme", "system"); if (!themePreference.matches("system|light|dark")) themePreference = "system"; getPreferences(MODE_PRIVATE).edit().putString("theme", themePreference).putBoolean("sound", value.optBoolean("sound", true)).apply(); runOnUiThread(() -> { PushCoordinator.preferences(MainActivity.this, value.optBoolean("messages", true) || value.optBoolean("interactions", true)); updateSystemBars(); dispatch(new Json().put("type", "theme").put("theme", getTheme())); }); } catch (Exception ignored) { } }
        @JavascriptInterface public void playSound(String kind) { if (!getPreferences(MODE_PRIVATE).getBoolean("sound", true)) return; final int resource = "message".equals(kind) ? R.raw.notify_message : "request".equals(kind) ? R.raw.notify_request : "call-end".equals(kind) ? R.raw.notify_call_end : 0; if (resource == 0) return; runOnUiThread(() -> { MediaPlayer sound = MediaPlayer.create(MainActivity.this, resource); if (sound != null) { sound.setOnCompletionListener(MediaPlayer::release); sound.start(); } }); }
    }
    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == 41) {
            String call = pendingCall; pendingCall = null;
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED && call != null) new NativeBridge().startCall(call);
            else dispatch(new Json().put("type", "call").put("state", new Json().put("state", "error").put("code", "microphone_denied").put("message", "Microphone permission is required for calls")));
        }
        if (request == 42) PushCoordinator.permissionResult(this, results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED);
        if (request == 43) { String call = pendingCall; pendingCall = null; if (call != null) new NativeBridge().startCall(call); }
    }
}
