package com.speakeasy.app;

import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onBackPressed() {
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        String url = webView != null ? webView.getUrl() : null;
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
        if (!root && webView != null) {
            webView.evaluateJavascript("history.back()", null);
            return;
        }
        // 已经在首页：退到后台而不是直接杀掉
        moveTaskToBack(true);
    }
}
