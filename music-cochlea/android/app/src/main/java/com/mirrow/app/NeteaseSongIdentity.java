package com.mirrow.app;

import java.text.Normalizer;
import java.util.Locale;

/** Exact provider-backed title variants, never arbitrary parenthetical stripping. */
final class NeteaseSongIdentity {
    private static String normalized(String value) {
        return Normalizer.normalize(value == null ? "" : value, Normalizer.Form.NFKC)
                .replaceAll("\\s+", "").toLowerCase(Locale.ROOT);
    }
    static boolean matches(String title, String[] aliases, String actual) {
        String value = normalized(actual);
        if (value.isEmpty()) return false;
        if (value.equals(normalized(title))) return true;
        for (String alias : aliases) if (value.equals(normalized(alias))) return true;
        return false;
    }
}
