-- migrations/001_initial_schema.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Bảng Users (Admin/quản trị viên)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'admin' CHECK (role IN ('superadmin', 'admin', 'viewer')),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng Agents (Danh sách máy tính cài Agent)
CREATE TABLE agents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hostname VARCHAR(255) NOT NULL,
    ip_address VARCHAR(45),
    os_info VARCHAR(255),
    agent_version VARCHAR(50),
    status VARCHAR(20) DEFAULT 'inactive' CHECK (status IN ('active', 'inactive', 'disconnected')),
    department VARCHAR(100),
    last_heartbeat TIMESTAMP WITH TIME ZONE,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng Policies (Chính sách DLP)
CREATE TABLE policies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    -- Loại nhận dạng: 'keyword', 'regex', 'fingerprint'
    detection_type VARCHAR(50) NOT NULL CHECK (detection_type IN ('keyword', 'regex', 'fingerprint')),
    -- Cấu hình nhận dạng linh hoạt bằng JSON
    -- Ví dụ keyword: {"keywords": ["Bảng lương", "Chiến lược"]}
    -- Ví dụ regex: {"patterns": [{"name": "CCCD", "pattern": "\\d{12}"}, ...]}
    -- Ví dụ fingerprint: {"fingerprint_ids": ["uuid1", "uuid2"]}
    detection_config JSONB NOT NULL,
    -- Hành động: 'block', 'alert', 'log'
    action VARCHAR(20) NOT NULL CHECK (action IN ('block', 'alert', 'log')),
    -- Kênh giám sát: 'usb', 'browser', 'email', 'clipboard', 'all'
    target_channel VARCHAR(50) NOT NULL DEFAULT 'all',
    -- Phòng ban áp dụng
    target_departments JSONB DEFAULT '["all"]'::jsonb,
    is_active BOOLEAN DEFAULT TRUE,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng Alerts (Cảnh báo thời gian thực)
CREATE TABLE alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
    policy_id UUID REFERENCES policies(id) ON DELETE SET NULL,
    severity VARCHAR(20) DEFAULT 'medium' CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    channel VARCHAR(50) NOT NULL,
    file_name VARCHAR(500),
    file_path TEXT,
    matched_content TEXT, -- Nội dung khớp (trích đoạn)
    action_taken VARCHAR(20) NOT NULL,
    status VARCHAR(20) DEFAULT 'new' CHECK (status IN ('new', 'reviewed', 'resolved', 'false_positive')),
    triggered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng Activity Logs (Log chi tiết)
CREATE TABLE activity_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
    policy_id UUID REFERENCES policies(id) ON DELETE SET NULL,
    event_type VARCHAR(50) NOT NULL, -- 'file_copy', 'file_upload', 'email_attach', 'clipboard'
    channel VARCHAR(50) NOT NULL,
    file_name VARCHAR(500),
    details JSONB, -- Chi tiết bổ sung
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng Fingerprints (Vân tay dữ liệu)
CREATE TABLE fingerprints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    file_name VARCHAR(500) NOT NULL,
    file_hash VARCHAR(128) NOT NULL, -- SHA-256/512
    simhash_blocks JSONB, -- Lưu các khối hash cho so sánh từng phần
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes cho hiệu năng truy vấn
CREATE INDEX idx_alerts_triggered_at ON alerts(triggered_at DESC);
CREATE INDEX idx_alerts_agent_id ON alerts(agent_id);
CREATE INDEX idx_alerts_severity ON alerts(severity);
CREATE INDEX idx_alerts_status ON alerts(status);
CREATE INDEX idx_activity_logs_created_at ON activity_logs(created_at DESC);
CREATE INDEX idx_activity_logs_agent_id ON activity_logs(agent_id);
CREATE INDEX idx_agents_status ON agents(status);
CREATE INDEX idx_agents_department ON agents(department);
CREATE INDEX idx_policies_is_active ON policies(is_active);
