<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Agent Groups</h1>
        <p class="subtitle">
          Configure departments and manage their assigned agents.
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openGroupDialog()"
        >Create New Group</el-button
      >
    </div>
    <div class="header-actions">
      <el-dropdown
        @command="(format) => exportData(groups, 'all_groups', format)"
      >
        <el-button :icon="Download">Export All</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="json">Download JSON</el-dropdown-item>
            <el-dropdown-item command="yaml">Download YAML</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
    <el-card shadow="never">
      <div class="toolbar">
        <el-input
          v-model="searchQuery"
          placeholder="Search by name, ID or details..."
          :prefix-icon="Search"
          clearable
          style="width: 350px"
          @input="handleSearch"
        />
        <el-button :icon="Refresh" @click="fetchData">Reload</el-button>
      </div>

      <!-- GROUP LIST -->
      <el-table :data="groups" v-loading="loading">
        <el-table-column prop="name" label="GROUP NAME" min-width="200" />
        <el-table-column prop="member_count" label="AGENTS" width="120" />
        <el-table-column label="MANAGE" width="180" align="right">
          <template #default="{ row }">
            <el-dropdown
              @command="(format) => exportData(row, `group_${row.id}`, format)"
            >
              <el-button link :icon="Document" />
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="json">JSON</el-dropdown-item>
                  <el-dropdown-item command="yaml">YAML</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <el-button link :icon="Setting" @click="openGroupDialog(row)"
              >Manage</el-button
            >

            <el-button
              link
              type="danger"
              :icon="Delete"
              @click="handleDelete(row.id)"
            />
          </template>
        </el-table-column>
      </el-table>
      <!-- PAGINATION -->
      <div class="pagination-container">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          :total="total"
          @size-change="fetchData"
          @current-change="fetchData"
        />
      </div>
    </el-card>

    <!-- DIALOG: MANAGE GROUP (SETTINGS + MEMBERS) -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? `Manage: ${form.name}` : 'Create New Group'"
      width="650px"
    >
      <el-tabs v-model="activeTab" type="border-card">
        <!-- TAB 1: GENERAL INFO -->
        <el-tab-pane label="Settings" name="settings">
          <el-form :model="form" label-position="top">
            <el-form-item label="Group Name" required>
              <el-input v-model="form.name" />
            </el-form-item>
            <el-form-item label="Description">
              <el-input v-model="form.description" type="textarea" :rows="3" />
            </el-form-item>
            <div style="text-align: right; margin-top: 20px">
              <el-button
                type="primary"
                @click="submitGeneralInfo"
                :loading="submitting"
                >Save Settings</el-button
              >
            </div>
          </el-form>
        </el-tab-pane>

        <!-- TAB 2: MEMBERS MANAGEMENT  -->
        <el-tab-pane
          label="Members Management"
          name="members"
          :disabled="!isEdit"
        >
          <div class="member-header">
            <h4>Current Members ({{ form.agents?.length || 0 }})</h4>
            <div class="add-member-area">
              <el-select
                v-model="newMemberIds"
                multiple
                collapse-tags
                placeholder="Select unassigned agents to add"
                style="width: 300px"
              >
                <el-option
                  v-for="a in unassignedAgents"
                  :key="a.id"
                  :label="a.hostname"
                  :value="a.id"
                />
              </el-select>
              <el-button
                type="success"
                @click="handleAddMembers"
                :disabled="newMemberIds.length === 0"
                >Add</el-button
              >
            </div>
          </div>

          <el-table :data="form.agents" max-height="300" size="small">
            <el-table-column prop="hostname" label="Hostname" />
            <el-table-column prop="ip_address" label="IP" />
            <el-table-column label="" width="60">
              <template #default="scope">
                <el-button
                  link
                  type="danger"
                  :icon="Close"
                  @click="removeAgent(scope.row.id)"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- TAB 3: POLICIES -->
        <el-tab-pane
          label="Applied Policies"
          name="policies"
          :disabled="!isEdit"
        >
          <div class="member-header">
            <h4>Active Policies ({{ form.policies?.length || 0 }})</h4>
            <div class="add-member-area">
              <el-select
                v-model="newPolicyIds"
                multiple
                collapse-tags
                placeholder="Select policies to apply"
                style="width: 300px"
              >
                <el-option
                  v-for="p in availablePolicies"
                  :key="p.id"
                  :label="p.name"
                  :value="p.id"
                />
              </el-select>
              <el-button
                type="primary"
                :icon="Link"
                @click="handleAssignPolicies"
                :disabled="newPolicyIds.length === 0"
                >Apply</el-button
              >
            </div>
          </div>

          <el-table :data="form.policies" max-height="300" size="small">
            <el-table-column prop="name" label="Policy Name" min-width="200" />
            <el-table-column label="Action" width="100">
              <template #default="{ row }">
                <el-tag :type="getActionType(row.action)" effect="dark">
                  {{ row.action.toUpperCase() }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="" width="60" align="right">
              <template #default="scope">
                <el-button
                  link
                  type="danger"
                  :icon="Close"
                  @click="removePolicyFromGroup(scope.row.id)"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import {
  Plus,
  Setting,
  Delete,
  Close,
  Download,
  Document,
  ArrowDown,
  Search,
  Refresh,
  Link,
} from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { ElMessage, ElMessageBox } from "element-plus";
import { exportData } from "@/utils/exporter";

const loading = ref(false);
const submitting = ref(false);
const dialogVisible = ref(false);
const isEdit = ref(false);
const activeTab = ref("settings");

const groups = ref([]);
const unassignedAgents = ref([]);
const newMemberIds = ref([]);
const availablePolicies = ref([]);
const newPolicyIds = ref([]);

const form = ref({
  id: null,
  name: "",
  description: "",
  agents: [],
  policies: [],
});
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const searchQuery = ref("");

let searchTimer = null;

const fetchData = async () => {
  loading.value = true;
  try {
    const [gRes, aRes, pRes] = await Promise.all([
      apiClient.get(
        `/agent-groups/?page=${page.value}&page_size=${pageSize.value}&search=${searchQuery.value}`,
      ),
      apiClient.get("/agents/"),
      apiClient.get("/policies/"),
    ]);
    groups.value = gRes.data.items || gRes.data;
    availablePolicies.value = pRes.data.items || pRes.data;
    total.value = gRes.data.total || 0;

    const allAgents = aRes.data.items || aRes.data;
    unassignedAgents.value = allAgents.filter((a) => !a.group_id);
  } finally {
    loading.value = false;
  }
};

const handleSearch = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    fetchData();
  }, 500);
};

const handleAssignPolicies = async () => {
  try {
    // API : POST /api/v1/policies/{policy_id}/assign-groups
    const promises = newPolicyIds.value.map((policyId) =>
      apiClient.post(`/policies/${policyId}/assign-groups`, [form.value.id]),
    );

    await Promise.all(promises);
    ElMessage.success("Policies applied to group successfully");
    newPolicyIds.value = [];

    await refreshCurrentGroupData();
  } catch (e) {
    ElMessage.error("Failed to assign policies");
  }
};

const removePolicyFromGroup = async (policyId) => {
  ElMessageBox.confirm("Remove this policy from the group?").then(async () => {
    try {
      const res = await apiClient.get(`/policies/${policyId}`);

      const updatedGroups = currentGroups.filter((id) => id !== form.value.id);

      await apiClient.post(
        `/policies/${policyId}/assign-groups`,
        updatedGroups,
      );

      ElMessage.success("Policy removed from group");
      await refreshCurrentGroupData();
    } catch (e) {
      ElMessage.error("Failed to remove policy");
    }
  });
};

const refreshCurrentGroupData = async () => {
  const res = await apiClient.get("/agent-groups/");
  groups.value = res.data.items || res.data;
  const updated = groups.value.find((g) => g.id === form.value.id);
  if (updated) {
    form.value.policies = updated.policies || [];
    form.value.agents = updated.agents || [];
  }
};

const openGroupDialog = (row = null) => {
  if (row) {
    isEdit.value = true;
    form.value = { ...row };
    activeTab.value = "settings";
  } else {
    isEdit.value = false;
    form.value = { id: null, name: "", description: "", agents: [] };
    activeTab.value = "settings";
  }
  newMemberIds.value = [];
  dialogVisible.value = true;
};

const submitGeneralInfo = async () => {
  submitting.value = true;
  try {
    if (isEdit.value) {
      await apiClient.put(`/agent-groups/${form.value.id}`, {
        name: form.value.name,
        description: form.value.description,
      });
    } else {
      await apiClient.post("/agent-groups/", form.value);
    }
    ElMessage.success("Group information saved");
    fetchData();
    if (!isEdit.value) dialogVisible.value = false;
  } catch (e) {
    ElMessage.error("Failed to save");
  } finally {
    submitting.value = false;
  }
};

const handleAddMembers = async () => {
  try {
    await apiClient.post(`/agent-groups/${form.value.id}/members`, {
      agent_ids: newMemberIds.value,
    });
    ElMessage.success("Agents added to group");
    newMemberIds.value = [];
    const res = await apiClient.get("/agent-groups/");
    groups.value = res.data.items || res.data;
    const updated = groups.value.find((g) => g.id === form.value.id);
    if (updated) form.value.agents = updated.agents;
  } catch (e) {
    ElMessage.error("Add failed");
  }
};

const removeAgent = async (agentId) => {
  try {
    await apiClient.delete(`/agent-groups/${form.value.id}/members/${agentId}`);
    form.value.agents = form.value.agents.filter((a) => a.id !== agentId);
    fetchData();
    ElMessage.success("Agent removed from group");
  } catch (e) {
    ElMessage.error("Remove failed");
  }
};

const handleDelete = (id) => {
  ElMessageBox.confirm("Delete this group?").then(async () => {
    await apiClient.delete(`/agent-groups/${id}`);
    fetchData();
  });
};

onMounted(fetchData);

const getActionType = (action) => {
  const map = {
    block: "danger",
    alert: "warning",
    allow: "success",
  };
  return map[action?.toLowerCase()] || "info";
};
</script>

<style scoped>
.member-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
}
.add-member-area {
  display: flex;
  gap: 10px;
}
:deep(.el-tabs--border-card) {
  border: none;
  box-shadow: none;
}
.pagination-container {
  margin-top: 24px;
  display: flex;
  justify-content: flex-end;
  padding-bottom: 8px;
}

/* Làm cho Search Bar trông gi?ng VS Code hõn */
.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 20px;
}

.el-input__wrapper {
  background-color: #ffffff !important;
  box-shadow: 0 0 0 1px #e2e8f0 inset !important;
}

.el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px var(--primary-color) inset !important;
}
</style>
