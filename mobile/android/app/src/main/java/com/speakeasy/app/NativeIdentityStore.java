package com.speakeasy.app;

import android.content.Context;
import android.content.SharedPreferences;
import androidx.security.crypto.EncryptedSharedPreferences;
import androidx.security.crypto.MasterKey;
import org.json.JSONObject;

final class NativeIdentityStore {
    private static final String FILE = "speakeasy_secure_identity";
    private final SharedPreferences preferences;

    NativeIdentityStore(Context context) throws Exception {
        this(context, FILE);
    }
    NativeIdentityStore(Context context, String storageName) throws Exception {
        MasterKey key = new MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build();
        preferences = EncryptedSharedPreferences.create(context, storageName, key,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM);
    }

    JSONObject read() throws Exception {
        String raw = preferences.getString("identity", null);
        return raw == null ? null : new JSONObject(raw);
    }

    void save(JSONObject identity) throws Exception {
        if (identity == null || identity.optString("user_id").isEmpty() || identity.optString("token").isEmpty())
            throw new IllegalArgumentException("Identity requires user_id and token");
        preferences.edit().putString("identity", identity.toString()).apply();
    }
    String deviceKey() { return preferences.getString("device_key", null); }
    void saveDeviceKey(String key) {
        if (key != null && key.matches("[a-zA-Z0-9_-]{8,128}")) preferences.edit().putString("device_key", key).apply();
    }

    JSONObject readSession() throws Exception {
        String raw = preferences.getString("session", null);
        if (raw == null) return null;
        JSONObject session = new JSONObject(raw);
        return NativePolicy.validSession(session.optString("userId"), session.optString("sessionToken"),
                session.optString("expiresAt"), System.currentTimeMillis()) ? session : null;
    }

    boolean saveSession(JSONObject session) {
        if (session == null || !NativePolicy.validSession(session.optString("userId"), session.optString("sessionToken"),
                session.optString("expiresAt"), System.currentTimeMillis()))
            throw new IllegalArgumentException("Valid user, session and expiry required");
        return preferences.edit().putString("session", session.toString()).putBoolean("account_mode", true)
                .remove("identity").commit();
    }

    boolean clearSession() {
        return preferences.edit().remove("session").remove("identity").putBoolean("account_mode", true).commit();
    }

    boolean accountMode() { return preferences.getBoolean("account_mode", false); }

    synchronized String installationId() {
        String id = preferences.getString("installation_id", null);
        if (id == null) {
            id = java.util.UUID.randomUUID().toString();
            if (!preferences.edit().putString("installation_id", id).commit())
                throw new IllegalStateException("Secure installation storage unavailable");
        }
        return id;
    }

    JSONObject callIdentity() throws Exception {
        JSONObject session = readSession();
        if (session != null) return new JSONObject().put("user_id", session.getString("userId"))
                .put("session_token", session.getString("sessionToken"));
        if (accountMode()) return null;
        return read();
    }
}
