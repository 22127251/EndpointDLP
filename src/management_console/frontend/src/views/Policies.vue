<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Security Policies</h1>
        <p class="subtitle">Manage DLP rules and deployment targets.</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="handleOpenDialog()"
        >Create New Policy</el-button
      >
    </div>
    <div class="header-actions">
      <el-dropdown
        @command="(format) => exportData(policies, 'all_policies', format)"
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

    <!-- TABLE -->

    <el-card shadow="never">
      <!-- SEARCH BAR -->
      <div class="toolbar">
        <el-input
          v-model="searchQuery"
          placeholder="Search by name"
          :prefix-icon="Search"
          clearable
          style="width: 350px"
          @input="handleSearch"
        />
        <el-button :icon="Refresh" @click="fetchData">Reload</el-button>
      </div>

      <!-- POLICY TABLE -->
      <el-table :data="policies" v-loading="loading">
        <el-table-column prop="name" label="POLICY NAME" min-width="220" />
        <el-table-column label="CHANNEL" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{
              row.channel.toUpperCase()
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="ACTION" width="120">
          <template #default="{ row }">
            <el-tag :type="getActionType(row.action)" effect="dark">
              {{ row.action.toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="STATUS" width="100">
          <template #default="{ row }">
            <el-switch
              v-model="row.is_active"
              @change="handleToggleStatus(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="MANAGE" width="150" align="right">
          <template #default="{ row }">
            <el-dropdown
              @command="(format) => exportData(row, `policy_${row.id}`, format)"
            >
              <el-button link :icon="Document" />
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="json">JSON</el-dropdown-item>
                  <el-dropdown-item command="yaml">YAML</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>

            <el-button link :icon="Edit" @click="handleOpenDialog(row)"
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

    <!-- CREATE/EDIT DIALOG -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? 'Edit Policy' : 'Create New Policy'"
      width="650px"
    >
      <el-form :model="form" label-position="top">
        <el-form-item label="Policy Name" required>
          <el-input v-model="form.name" />
        </el-form-item>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="Channel">
              <el-select v-model="form.channel" style="width: 100%">
                <el-option
                  v-for="c in constants.channels"
                  :key="c"
                  :label="c.toUpperCase()"
                  :value="c"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Action">
              <el-select v-model="form.action" style="width: 100%">
                <el-option
                  v-for="a in constants.actions"
                  :key="a"
                  :label="a.toUpperCase()"
                  :value="a"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <!-- RULE (DYNAMIC JSON) -->
        <div class="rule-section">
          <div class="section-label">Rule (JSON key-value pairs)</div>

          <div v-for="(row, index) in ruleRows" :key="index" class="rule-row">
            <el-input
              v-model="row.key"
              placeholder="Key (e.g. pattern)"
              style="width: 35%"
            />
            <span class="separator">:</span>
            <el-input
              v-model="row.value"
              placeholder="Value (e.g. \b\d{16}\b)"
              style="width: 55%"
            />

            <el-button
              type="danger"
              :icon="Delete"
              circle
              size="small"
              @click="removeRuleRow(index)"
              :disabled="ruleRows.length === 1"
            />
          </div>

          <el-button
            type="primary"
            link
            :icon="Plus"
            @click="addRuleRow"
            class="mt-2"
          >
            Add Property
          </el-button>
        </div>

        <el-form-item label="Description" class="mt-4">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">Cancel</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit"
          >Save Policy</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import {
  Plus,
  Edit,
  Delete,
  Download,
  Document,
  Search,
  Refresh,
} from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { ElMessage, ElMessageBox } from "element-plus";
import { exportData } from "@/utils/exporter";

const loading = ref(false);
const submitting = ref(false);
const dialogVisible = ref(false);
const isEdit = ref(false);
const policies = ref([]);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const searchQuery = ref("");

const handleExport = (format) => {
  const dataToExport = policies.value;
  exportData(dataToExport, "policies", format);
};

// Metadata constants from Backend
const constants = ref({ actions: [], channels: [], rule_types: [] });

const form = ref({
  id: null,
  name: "",
  description: "",
  rule_type: "regex",
  rule: {},
  action: "block",
  channel: "all",
  is_active: true,
});

// Dynamic rule key-value pairs for form input
const ruleRows = ref([{ key: "", value: "" }]);
const addRuleRow = () => {
  ruleRows.value.push({ key: "", value: "" });
};
const removeRuleRow = (index) => {
  ruleRows.value.splice(index, 1);
};

let searchTimer = null;

const fetchData = async () => {
  loading.value = true;
  try {
    const [pRes, mRes] = await Promise.all([
      apiClient.get("/policies/", {
        params: {
          page: page.value,
          page_size: pageSize.value,
          search: searchQuery.value,
        },
      }),
      apiClient.get("/metadata/constants"),
    ]);

    policies.value = pRes.data.items || pRes.data;
    total.value = pRes.data.total || 0;
    constants.value = {
      actions: mRes.data.policy_actions || [],
      channels: mRes.data.policy_channels || [],
      rule_types: mRes.data.rule_types || [],
    };
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

const prepareRuleToRows = (ruleObj) => {
  const rows = Object.entries(ruleObj).map(([key, value]) => ({ key, value }));
  ruleRows.value = rows.length > 0 ? rows : [{ key: "", value: "" }];
};

const prepareRowsToRule = () => {
  const obj = {};
  ruleRows.value.forEach((row) => {
    if (row.key.trim()) {
      obj[row.key.trim()] = row.value;
    }
  });
  return obj;
};

const handleOpenDialog = (row = null) => {
  if (row) {
    isEdit.value = true;
    form.value = { ...row };
    prepareRuleToRows(row.rule);
  } else {
    isEdit.value = false;
    form.value = {
      name: "",
      description: "",
      rule_type: "regex",
      action: "block",
      channel: "all",
      is_active: true,
    };
    ruleRows.value = [{ key: "", value: "" }];
  }
  dialogVisible.value = true;
};

const handleSubmit = async () => {
  submitting.value = true;

  form.value.rule = prepareRowsToRule();

  try {
    if (isEdit.value) {
      await apiClient.put(`/policies/${form.value.id}`, form.value);
      ElMessage.success("Policy updated");
    } else {
      await apiClient.post("/policies/", form.value);
      ElMessage.success("Policy created");
    }
    dialogVisible.value = false;
    fetchData();
  } catch (e) {
    ElMessage.error("Error saving policy");
  } finally {
    submitting.value = false;
  }
};

const handleToggleStatus = async (row) => {
  try {
    await apiClient.put(`/policies/${row.id}`, { is_active: row.is_active });
    ElMessage.success(`Policy ${row.is_active ? "enabled" : "disabled"}`);
  } catch (e) {
    row.is_active = !row.is_active;
  }
};

const handleDelete = (id) => {
  ElMessageBox.confirm("Delete this policy permanently?").then(async () => {
    await apiClient.delete(`/policies/${id}`);
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
.rule-section {
  background: #f8fafc;
  padding: 16px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  margin-top: 10px;
}
.section-label {
  font-size: 13px;
  font-weight: 600;
  color: #64748b;
  margin-bottom: 12px;
}
.rule-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.separator {
  font-weight: bold;
  color: #94a3b8;
}
.mt-2 {
  margin-top: 8px;
}
.mt-4 {
  margin-top: 16px;
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
