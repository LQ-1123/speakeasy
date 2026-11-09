package com.speakeasy.app;

import org.json.JSONObject;

/** Internal event construction; values are primitives, strings, and JSONObject instances. */
final class Json extends JSONObject {
    @Override public Json put(String key, Object value) {
        try { super.put(key, value); return this; }
        catch (org.json.JSONException impossible) { throw new IllegalArgumentException(impossible); }
    }
    @Override public Json put(String key, int value) { return put(key, (Object) value); }
    @Override public Json put(String key, long value) { return put(key, (Object) value); }
    @Override public Json put(String key, boolean value) { return put(key, (Object) value); }
}
