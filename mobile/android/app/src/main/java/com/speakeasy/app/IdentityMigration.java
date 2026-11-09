package com.speakeasy.app;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.os.Handler;
import android.os.Looper;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import org.json.JSONArray;
import org.json.JSONObject;

/** Reads old origin storage using our own script. Never loads remote code or exposes a JS bridge. */
final class IdentityMigration {
    interface Result { void complete(String state, String error); }
    private final Context context;
    private final NativeIdentityStore store;
    private final Result callback;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final String[] origins;
    private int index;
    private WebView probe;
    private boolean finished;
    private boolean error;
    IdentityMigration(Context context, NativeIdentityStore store, Result callback) {
        this(context, store, callback, new String[]{"https://localhost", "http://localhost", BuildConfig.API_BASE, "http://47.104.24.236:8000"});
    }
    IdentityMigration(Context context, NativeIdentityStore store, Result callback, String[] origins) { this.context = context; this.store = store; this.callback = callback; this.origins = origins; }
    void run() {
        try { if (store.read() != null) { finish("ready", ""); return; } }
        catch (Exception failure) { finish("failed", "Secure identity storage is unreadable"); return; }
        next();
    }
    private void next() {
        if (finished) return;
        if (index >= origins.length) {
            try {
                if (store.deviceKey() != null) { recoverDeviceKey(store.deviceKey()); return; }
                PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
                boolean upgraded = info.lastUpdateTime > info.firstInstallTime + 5_000;
                finish(upgraded || error ? "failed" : "new", upgraded ? "Could not recover your existing identity. Retry migration before creating a new account." : "");
            } catch (Exception failure) { finish("failed", "Identity migration could not be completed"); }
            return;
        }
        String origin = origins[index++];
        probe = new WebView(context);
        probe.getSettings().setJavaScriptEnabled(true);
        probe.getSettings().setDomStorageEnabled(true);
        WebView view = probe;
        final boolean[] received = {false};
        view.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, String url) { return true; }
            @Override public void onPageFinished(WebView view, String url) {
                view.evaluateJavascript("(function(){try{return JSON.stringify([localStorage.getItem('speakeasy.identity.v1'),localStorage.getItem('speakeasy.device_key.v1')])}catch(e){return 'error'}})()", result -> {
                    if (received[0] || finished) return; received[0] = true;
                    try {
                        Object value = new org.json.JSONTokener(result).nextValue();
                        JSONArray values = new JSONArray((String) value);
                        String identity = values.isNull(0) ? null : values.getString(0);
                        String key = values.isNull(1) ? null : values.getString(1);
                        if (key != null) store.saveDeviceKey(new org.json.JSONTokener(key).nextValue().toString());
                        if (identity != null) store.save(new JSONObject(identity));
                    } catch (Exception failure) { error = true; }
                    view.destroy();
                    try { if (store.read() != null) { finish("ready", ""); return; } } catch (Exception ignored) { }
                    next();
                });
            }
        });
        // Inline data has the historic security origin, but no outgoing request, token URL, or remote JS.
        view.loadDataWithBaseURL(origin + "/", "<!doctype html><meta charset=utf-8><title>Identity migration</title>", "text/html", "UTF-8", null);
        ui.postDelayed(() -> { if (!received[0] && !finished) { received[0] = true; error = true; view.destroy(); next(); } }, 2_000);
    }
    private void recoverDeviceKey(String key) {
        new Thread(() -> {
            okhttp3.OkHttpClient http = new okhttp3.OkHttpClient.Builder().followRedirects(false).connectTimeout(5, java.util.concurrent.TimeUnit.SECONDS).callTimeout(8, java.util.concurrent.TimeUnit.SECONDS).build();
            try {
                okhttp3.Request request = new okhttp3.Request.Builder().url(BuildConfig.API_BASE + "/api/v3/auth/register")
                        .post(okhttp3.RequestBody.create(new Json().put("device_key", key).toString(), okhttp3.MediaType.get("application/json"))).build();
                try (okhttp3.Response response = http.newCall(request).execute()) {
                    if (!response.isSuccessful() || response.body() == null) throw new IllegalStateException("Cannot restore existing account while offline");
                    store.save(new JSONObject(response.body().string()));
                    ui.post(() -> finish("ready", ""));
                }
            } catch (Exception failure) { ui.post(() -> finish("failed", "Could not restore your existing account. Connect to the network and reopen the app.")); }
        }, "SpeakEasyIdentityMigration").start();
    }
    private void finish(String state, String message) { if (finished) return; finished = true; callback.complete(state, message); }
}
