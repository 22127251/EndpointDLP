<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Agent Management</h1>
        <p class="subtitle">
          Monitor status and organize machines into security groups.
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openRegisterDialog"
        >Create New Agent</el-button
      >
    </div>

    <div class="header-actions">
      <el-dropdown
        @command="(format) => exportData(agents, 'agents_list', format)"
      >
        <el-button :icon="Download">Export All </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="json">JSON</el-dropdown-item>
            <el-dropdown-item command="yaml">YAML</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <!-- MAIN TABLE -->
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
      <el-table :data="agents" v-loading="loading" style="width: 100%">
        <el-table-column label="COMPUTER NAME" min-width="200">
          <template #default="{ row }">
            <div class="agent-info">
              <div>
                <div class="hostname">{{ row.hostname || "Unregistered" }}</div>
                <div class="uuid">{{ row.id }}</div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="STATUS" width="150">
          <template #default="{ row }">
            <el-tag
              :type="getStatusType(row.status)"
              size="small"
              effect="light"
            >
              <span class="dot" :class="row.status"></span>
              {{ row.status.toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="GROUP" width="180">
          <template #default="{ row }">
            <el-tag
              v-if="row.group_id"
              type="success"
              effect="plain"
              class="p-tag"
            >
              {{ getGroupName(row.group_id) }}
            </el-tag>
            <span v-else class="text-muted">Unassigned</span>
          </template>
        </el-table-column>

        <el-table-column label="MANAGE" width="220" align="right">
          <template #default="{ row }">
            <el-dropdown
              @command="(format) => exportData(row, `agent_${row.id}`, format)"
            >
              <el-button link :icon="Document" />
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="json">JSON</el-dropdown-item>
                  <el-dropdown-item command="yaml">YAML</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <el-tooltip content="Assign to Group" placement="top">
              <el-button link :icon="FolderAdd" @click="openGroupDialog(row)" />
            </el-tooltip>
            <el-button link :icon="Edit" @click="openEditDialog(row)"
              >Edit</el-button
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

    <!-- DIALOG: REGISTER NEW AGENT -->
    <el-dialog
      v-model="registerVisible"
      title="Register New Agent"
      width="400px"
    >
      <el-form :model="registerForm" label-position="top">
        <el-form-item label="Hostname (Required)" required>
          <el-input
            v-model="registerForm.hostname"
            placeholder="e.g. PC-OFFICE-01"
          />
        </el-form-item>
        <el-form-item label="Initial Status">
          <el-select v-model="registerForm.status" style="width: 100%">
            <el-option label="Active" value="active" />
            <el-option label="Inactive" value="inactive" />
          </el-select>
        </el-form-item>
        <el-form-item label="Description">
          <el-input v-model="registerForm.description" type="textarea" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="registerVisible = false">Cancel</el-button>
        <el-button type="primary" @click="submitRegister" :loading="submitting"
          >Register</el-button
        >
      </template>
    </el-dialog>

    <!-- DIALOG: EDIT STATUS & HOSTNAME -->
    <el-dialog v-model="editVisible" title="Update Agent Info" width="400px">
      <el-form :model="editForm" label-position="top">
        <el-form-item label="Hostname">
          <el-input v-model="editForm.hostname" />
        </el-form-item>
        <el-form-item label="Operational Status">
          <el-select v-model="editForm.status" style="width: 100%">
            <el-option label="Active" value="active" />
            <el-option label="Inactive" value="inactive" />
            <el-option label="Offline" value="offline" />
          </el-select>
        </el-form-item>
        <el-form-item label="Description">
          <el-input v-model="editForm.description" type="textarea" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">Cancel</el-button>
        <el-button type="primary" @click="submitUpdate" :loading="submitting"
          >Update</el-button
        >
      </template>
    </el-dialog>

    <!-- DIALOG: ASSIGN TO GROUP -->
    <el-dialog
      v-model="groupVisible"
      title="Assign Agent to Group"
      width="400px"
    >
      <div class="assign-info">
        Assign
        <b>{{ activeAgent?.hostname || activeAgent?.id.slice(0, 8) }}</b> to:
      </div>
      <el-select
        v-model="targetGroupId"
        placeholder="Select Group"
        style="width: 100%; margin-top: 15px"
      >
        <el-option
          v-for="g in groups"
          :key="g.id"
          :label="g.name"
          :value="g.id"
        />
      </el-select>
      <template #footer>
        <el-button @click="groupVisible = false">Cancel</el-button>
        <el-button
          type="primary"
          @click="submitGroupAssignment"
          :loading="submitting"
          >Assign</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import {
  Plus,
  Monitor,
  Edit,
  Delete,
  FolderAdd,
  Document,
  Download,
  ArrowDown,
  Search,
  Refresh,
} from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { exportData } from "@/utils/exporter";
import { ElMessage, ElMessageBox } from "element-plus";

const loading = ref(false);
const submitting = ref(false);
const agents = ref([]);
const groups = ref([]);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const searchQuery = ref("");

// Edit Logic
const editVisible = ref(false);
const editForm = ref({ id: "", hostname: "", status: "", description: "" });

// Group Logic
const groupVisible = ref(false);
const activeAgent = ref(null);
const targetGroupId = ref("");

let searchTimer = null;

const fetchData = async () => {
  loading.value = true;
  try {
    const [aRes, gRes] = await Promise.all([
      apiClient.get("/agents/", {
        params: {
          page: page.value,
          page_size: pageSize.value,
          search: searchQuery.value,
        },
      }),
      apiClient.get("/agent-groups/"),
    ]);
    // OpenAPI typically wraps list data in .items
    agents.value = aRes.data.items || aRes.data;
    groups.value = gRes.data.items || gRes.data;
    total.value = aRes.data.total || 0;
  } catch (error) {
    console.error("Fetch error:", error);
    agents.value = [];
    groups.value = [];
    ElMessage.error("Failed to load data from server");
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

const getStatusType = (status) => {
  if (status === "active") return "success";
  if (status === "offline") return "info";
  return "warning";
};

const getGroupName = (id) =>
  groups.value.find((g) => g.id === id)?.name || "N/A";

// Action: Edit
const openEditDialog = (row) => {
  editForm.value = { ...row };
  editVisible.value = true;
};

const submitUpdate = async () => {
  submitting.value = true;
  try {
    // API: PATCH /api/v1/agents/{agent_id}
    await apiClient.patch(`/agents/${editForm.value.id}`, {
      hostname: editForm.value.hostname,
      status: editForm.value.status,
      description: editForm.value.description,
    });
    ElMessage.success("Agent status updated");
    editVisible.value = false;
    fetchData();
  } finally {
    submitting.value = false;
  }
};

// Action: Assign to Group
const openGroupDialog = (row) => {
  activeAgent.value = row;
  targetGroupId.value = row.group_id || "";
  groupVisible.value = true;
};

const submitGroupAssignment = async () => {
  if (!targetGroupId.value) return;
  submitting.value = true;
  try {
    // API: POST /api/v1/agent-groups/{group_id}/members
    // Payload: { agent_ids: [uuid] }
    await apiClient.post(`/agent-groups/${targetGroupId.value}/members`, {
      agent_ids: [activeAgent.value.id],
    });
    ElMessage.success("Agent assigned to group");
    groupVisible.value = false;
    fetchData();
  } finally {
    submitting.value = false;
  }
};

const registerVisible = ref(false);
const registerForm = ref({
  hostname: "",
  status: "active",
  description: "",
  group_id: null,
});

const openRegisterDialog = () => {
  registerForm.value = {
    hostname: "",
    status: "active",
    description: "",
    group_id: null,
  };
  registerVisible.value = true;
};

const submitRegister = async () => {
  if (!registerForm.value.hostname) {
    return ElMessage.warning("Hostname is required by the database!");
  }

  submitting.value = true;
  try {
    // API: POST /api/v1/agents/register
    await apiClient.post("/agents/register", registerForm.value);

    ElMessage.success("Agent registered successfully");
    registerVisible.value = false;
    fetchData();
  } catch (error) {
    console.error(error);
    ElMessage.error(error.response?.data?.detail || "Registration failed");
  } finally {
    submitting.value = false;
  }
};

const handleDelete = (id) => {
  ElMessageBox.confirm("Remove agent?").then(async () => {
    await apiClient.delete(`/agents/${id}`);
    fetchData();
  });
};

onMounted(fetchData);
</script>

<style scoped>
.agent-info {
  display: flex;
  align-items: center;
  gap: 12px;
}

.hostname {
  font-weight: 600;
  color: #1e293b;
}
.uuid {
  font-size: 10px;
  color: #94a3b8;
  font-family: monospace;
}
.status-cell .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  margin-right: 6px;
}
.dot.active {
  background: #10b981;
}
.dot.offline {
  background: #94a3b8;
}
.dot.inactive {
  background: #f59e0b;
}
.assign-info {
  font-size: 14px;
  color: #64748b;
}
.pagination-container {
  margin-top: 24px;
  display: flex;
  justify-content: flex-end;
  padding-bottom: 8px;
}

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
