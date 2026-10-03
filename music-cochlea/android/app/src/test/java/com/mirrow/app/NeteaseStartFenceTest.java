package com.mirrow.app;

import org.junit.Test;
import static org.junit.Assert.assertEquals;

public class NeteaseStartFenceTest {
    @Test public void oldPlayingSongDoesNotReleasePendingGuard() {
        assertEquals(NeteaseStartFence.Decision.WAIT, NeteaseStartFence.evaluate(30000, 1000, false, true));
    }
    @Test public void matchingPausedMetadataIsNotPlaybackEvidence() {
        assertEquals(NeteaseStartFence.Decision.WAIT, NeteaseStartFence.evaluate(30000, 1000, true, false));
    }
    @Test public void lateMatchingPlaybackConfirmsWithinBound() {
        assertEquals(NeteaseStartFence.Decision.CONFIRMED, NeteaseStartFence.evaluate(30000, 20000, true, true));
    }
    @Test public void expiredOrRestoredDeadlineCannotBeExtendedByMetadata() {
        assertEquals(NeteaseStartFence.Decision.EXPIRED, NeteaseStartFence.evaluate(30000, 30000, true, true));
        assertEquals(NeteaseStartFence.Decision.EXPIRED, NeteaseStartFence.evaluate(30000, 31000, false, false));
    }
}
