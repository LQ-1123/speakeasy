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
                // 允许自动播放（否则异步加载音频后 play() 会被拦截）
                s.setMediaPlaybackRequiresUserGesture(false);
                // 允许 https://localhost 页面加载 http://IP 的音频/接口
                s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
            }
        } catch (Throwable ignored) {
            // 设置失败不影响 App 启动
        }
    }

    @Override
    public void onBackPressed() {
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        if (webView == null) {
            super.onBackPressed();
            return;
        }
        // 先问页面：如果有浮层（好友详情等）打开，由页面自己关闭；返回 handled 表示已处理
        webView.evaluateJavascript(
                "(window.w4HandleBack ? window.w4HandleBack() : 'unhandled')",
                value -> {
                    if (value != null && value.contains("handled")) return;
                    String url = webView.getUrl();
                    String hash = "";
                    if (url != null) {
                        int i = url.indexOf('#');
                        if (i >= 0) hash = url.substring(i);
                    }
                    boolean root =
                            hash.isEmpty()
                                    || hash.equals("#/")
                                    || hash.equals("#/chats")
                                    || hash.equals("#/contacts")
                                    || hash.equals("#/discover")
                                    || hash.equals("#/expressions")
                                    || hash.equals("#/me");
                    if (!root) {
                        webView.evaluateJavascript("history.back()", null);
                        return;
                    }
                    moveTaskToBack(true);
                });
    }
}
