package com.speakeasy.app;

import android.content.Context;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class IdentityInstrumentationTest {
    @Test public void encryptedIdentitySurvivesRecreationAndInvalidWritePreservesAccount() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String name = "identity_instrumentation_" + System.nanoTime();
        try {
            NativeIdentityStore store = new NativeIdentityStore(context, name);
            store.save(new JSONObject().put("user_id", "fixture-user").put("token", "fixture-token"));
            assertEquals("fixture-user", new NativeIdentityStore(context, name).read().getString("user_id"));
            assertThrows(IllegalArgumentException.class, () -> store.save(new JSONObject().put("user_id", "replacement")));
            assertEquals("fixture-user", store.read().getString("user_id"));
            assertFalse(context.getSharedPreferences(name, Context.MODE_PRIVATE).getAll().toString().contains("fixture-token"));
        } finally { context.deleteSharedPreferences(name); }
    }

    @Test public void controlledHistoricOriginProbeRestoresStoredIdentityWithoutRemotePage() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String name = "identity_migration_" + System.nanoTime();
        String origin = "https://fixture-" + System.nanoTime() + ".invalid";
        NativeIdentityStore store = new NativeIdentityStore(context, name);
        CountDownLatch seeded = new CountDownLatch(1);
        CountDownLatch migrated = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<>();
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            WebView fixture = new WebView(context);
            fixture.getSettings().setJavaScriptEnabled(true); fixture.getSettings().setDomStorageEnabled(true);
            fixture.setWebViewClient(new WebViewClient() { @Override public void onPageFinished(WebView view, String url) {
                view.evaluateJavascript("localStorage.setItem('speakeasy.identity.v1',JSON.stringify({user_id:'migrated-user',token:'migration-token'}))", value -> { fixture.destroy(); seeded.countDown(); });
            }});
            fixture.loadDataWithBaseURL(origin + "/", "<!doctype html><title>Fixture</title>", "text/html", "UTF-8", null);
        });
        assertTrue("origin storage initialized", seeded.await(10, TimeUnit.SECONDS));
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> new IdentityMigration(context, store, (state, error) -> { result.set(state); migrated.countDown(); }, new String[]{origin}).run());
        try {
            assertTrue("migration finished", migrated.await(10, TimeUnit.SECONDS));
            assertEquals("ready", result.get());
            assertEquals("migrated-user", store.read().getString("user_id"));
        } finally { context.deleteSharedPreferences(name); }
    }
}
