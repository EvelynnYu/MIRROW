package com.mirrow.app;

/** Admission for a dispatched song, independent of Android media transport. */
final class NeteaseStartFence {
    static final long GRACE_MS = 30000L;
    enum Decision { WAIT, CONFIRMED, EXPIRED }

    static Decision evaluate(long deadline, long now, boolean identityMatches, boolean playing) {
        if (now >= deadline) return Decision.EXPIRED;
        return identityMatches && playing ? Decision.CONFIRMED : Decision.WAIT;
    }
}
