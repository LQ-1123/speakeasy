package com.speakeasy.app;

import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            WebView webView = getBridge() != null ? getBridge().getWebView() : null;
            if (webView != null) {
                WebSettings s = webView.getSettings();
                s.setMediaPlaybackRequiresUserGesture(false);
                s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
            }
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onBackPressed() {
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        if (webView == null) {
            super.onBackPressed();
            return;
        }
        // 顺序：页面浮层（如好友详情）→ 浏览器历史（从哪来回哪去）→ 退到后台
        String js =
                "(function(){"
                        + "if (window.w4HandleBack && window.w4HandleBack() === 'handled') return 'handled';"
                        + "if (window.history.length > 1) { window.history.back(); return 'handled'; }"
                        + "return 'unhandled';"
                        + "})()";
        webView.evaluateJavascript(
                js,
                value -> {
                    if (value != null && value.contains("handled")) return;
                    moveTaskToBack(true);
                });
    }
}