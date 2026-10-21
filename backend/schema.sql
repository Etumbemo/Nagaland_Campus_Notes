CREATE DATABASE IF NOT EXISTS campus_notes
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE campus_notes;

CREATE TABLE IF NOT EXISTS departments (
    id CHAR(36) PRIMARY KEY,
    name VARCHAR(160) NOT NULL UNIQUE,
    code VARCHAR(32) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS profiles (
    id CHAR(36) PRIMARY KEY,
    email VARCHAR(254) NOT NULL UNIQUE,
    username VARCHAR(80) NULL UNIQUE,
    name VARCHAR(160) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'user',
    token_version INT UNSIGNED NOT NULL DEFAULT 0,
    approval_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    institute_name VARCHAR(200) NULL,
    phone VARCHAR(32) NULL,
    designation VARCHAR(120) NULL,
    institute_document_path VARCHAR(500) NULL,
    institute_document_name VARCHAR(255) NULL,
    department_id CHAR(36) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_profiles_department (department_id),
    CONSTRAINT fk_profiles_department FOREIGN KEY (department_id)
        REFERENCES departments(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS password_reset_otps (
    email VARCHAR(254) PRIMARY KEY,
    user_id CHAR(36) NULL,
    code_hash CHAR(64) NULL,
    expires_at DATETIME NULL,
    resend_after DATETIME NOT NULL,
    attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    used_at DATETIME NULL,
    CONSTRAINT fk_password_reset_user FOREIGN KEY (user_id)
        REFERENCES profiles(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS papers (
    id CHAR(36) PRIMARY KEY,
    subject VARCHAR(200) NOT NULL,
    subject_code VARCHAR(80) NULL,
    department_id CHAR(36) NOT NULL,
    semester INT NOT NULL,
    exam_type VARCHAR(80) NOT NULL,
    year VARCHAR(8) NOT NULL,
    description TEXT NULL,
    file_url VARCHAR(500) NULL,
    uploaded_by CHAR(36) NULL,
    uploaded_by_name VARCHAR(160) NULL,
    views INT NOT NULL DEFAULT 0,
    downloads INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_papers_department (department_id),
    INDEX ix_papers_subject (subject),
    INDEX ix_papers_subject_code (subject_code),
    CONSTRAINT fk_papers_department FOREIGN KEY (department_id)
        REFERENCES departments(id) ON DELETE CASCADE,
    CONSTRAINT fk_papers_uploader FOREIGN KEY (uploaded_by)
        REFERENCES profiles(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS favorites (
    id CHAR(36) PRIMARY KEY,
    user_id CHAR(36) NOT NULL,
    paper_id CHAR(36) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_favorite_user_paper (user_id, paper_id),
    INDEX ix_favorites_paper (paper_id),
    CONSTRAINT fk_favorites_user FOREIGN KEY (user_id)
        REFERENCES profiles(id) ON DELETE CASCADE,
    CONSTRAINT fk_favorites_paper FOREIGN KEY (paper_id)
        REFERENCES papers(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS activity_logs (
    id CHAR(36) PRIMARY KEY,
    type VARCHAR(80) NOT NULL,
    message VARCHAR(500) NOT NULL,
    user_id CHAR(36) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX ix_activity_user (user_id),
    CONSTRAINT fk_activity_user FOREIGN KEY (user_id)
        REFERENCES profiles(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
