-- migrations/002_add_groups.sql

-- ============================================
-- AGENT GROUPS (Nhóm Agent theo phòng ban/chức năng)
-- ============================================
CREATE TABLE agent_groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    -- Hỗ trợ nhóm con (nested groups)
    -- VD: "Công ty" → "Phòng Kinh doanh" → "Team Bán hàng Online"
    parent_id UUID REFERENCES agent_groups(id) ON DELETE SET NULL,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng trung gian: Agent ↔ AgentGroup (Many-to-Many)
-- 1 Agent có thể thuộc NHIỀU group (vừa "Phòng KD" vừa "Nhóm VIP")
CREATE TABLE agent_group_members (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_group_id UUID NOT NULL REFERENCES agent_groups(id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    -- Mỗi agent chỉ xuất hiện 1 lần trong 1 group
    UNIQUE(agent_group_id, agent_id)
);

-- ============================================
-- POLICY GROUPS (Nhóm Policy theo mục đích)
-- ============================================
CREATE TABLE policy_groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    -- Độ ưu tiên khi có xung đột giữa các group
    priority VARCHAR(20) DEFAULT 'medium' 
        CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Bảng trung gian: Policy ↔ PolicyGroup (Many-to-Many)
CREATE TABLE policy_group_members (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    policy_group_id UUID NOT NULL REFERENCES policy_groups(id) ON DELETE CASCADE,
    policy_id UUID NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    -- Thứ tự thực thi trong group (policy nào check trước)
    execution_order INT DEFAULT 0,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(policy_group_id, policy_id)
);

-- ============================================
-- POLICY ASSIGNMENTS (Gán PolicyGroup → AgentGroup)
-- Đây là bảng TRUNG TÂM kết nối tất cả
-- ============================================
CREATE TABLE policy_assignments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    policy_group_id UUID NOT NULL REFERENCES policy_groups(id) ON DELETE CASCADE,
    agent_group_id UUID NOT NULL REFERENCES agent_groups(id) ON DELETE CASCADE,
    is_active BOOLEAN DEFAULT TRUE,
    assigned_by UUID REFERENCES users(id),
    assigned_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(policy_group_id, agent_group_id)
);

-- Indexes
CREATE INDEX idx_agm_agent_id ON agent_group_members(agent_id);
CREATE INDEX idx_agm_group_id ON agent_group_members(agent_group_id);
CREATE INDEX idx_pgm_policy_id ON policy_group_members(policy_id);
CREATE INDEX idx_pgm_group_id ON policy_group_members(policy_group_id);
CREATE INDEX idx_pa_agent_group ON policy_assignments(agent_group_id);
CREATE INDEX idx_pa_policy_group ON policy_assignments(policy_group_id);
CREATE INDEX idx_pa_is_active ON policy_assignments(is_active);
