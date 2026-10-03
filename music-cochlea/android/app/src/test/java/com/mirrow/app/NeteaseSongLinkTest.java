package com.mirrow.app;

import org.junit.Test;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

public class NeteaseSongLinkTest {
    @Test public void androidUsesSongPathNotDesktopPayload() {
        assertEquals("orpheus://song/1357815628",
                NeteaseMusicController.songDeepLink("1357815628"));
    }

    @Test public void rejectsInvalidIds() {
        for (String value : new String[] {null, "", "1/2", "1?play=1", "-1", " 1", "１２"}) {
            assertThrows(IllegalArgumentException.class,
                    () -> NeteaseMusicController.songDeepLink(value));
        }
    }
}
