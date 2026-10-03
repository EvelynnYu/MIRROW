package com.mirrow.app;
import org.junit.Test;
import static org.junit.Assert.*;

public class NeteaseSongIdentityTest {
    @Test public void matchesOnlyKnownTranslationVariants() {
        String[] aliases = {"My Jinji (我的金桔)"};
        assertTrue(NeteaseSongIdentity.matches("My Jinji", aliases, "My Jinji（我的金桔）"));
        assertTrue(NeteaseSongIdentity.matches("My Jinji", aliases, "my jinji"));
        assertFalse(NeteaseSongIdentity.matches("My Jinji", aliases, "My Jinji (Live)"));
        assertFalse(NeteaseSongIdentity.matches("My Jinji", aliases, ""));
    }
}
