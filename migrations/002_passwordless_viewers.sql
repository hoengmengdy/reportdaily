ALTER TABLE users ADD COLUMN passwordless INTEGER NOT NULL DEFAULT 0
    CHECK (passwordless IN (0, 1) AND (passwordless = 0 OR role = 'viewer'));
