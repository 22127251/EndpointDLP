<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Violation Logs</h1>
        <p class="subtitle">
          Detailed records of all detected security policy violations.
        </p>
      </div>
      <div class="header-actions">
        <!-- Export Logs -->
        <el-dropdown @command="handleExport">
          <el-button :icon="Download">Export Logs</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="json">Download JSON</el-dropdown-item>
              <el-dropdown-item command="yaml">Download YAML</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-card shadow="never">
      <!-- SEARCH BAR -->
      <div class="toolbar">
        <el-input
          v-model="searchQuery"
          placeholder="Search by action"
          :prefix-icon="Search"
          clearable
          style="width: 350px"
          @input="handleSearch"
        />
        <el-button :icon="Refresh" @click="fetchData">Reload</el-button>
      </div>

      <el-table :data="logs" v-loading="loading" class="custom-table">
        <el-table-column prop="timestamp" label="TIME" width="180">
          <template #default="{ row }">
            {{ new Date(row.created_at).toLocaleString() }}
          </template>
        </el-table-column>

        <el-table-column label="AGENT / USER" min-width="180">
          <template #default="{ row }">
            <div class="agent-info">
              <span class="hostname">{{
                row.agent?.hostname || "Unknown"
              }}</span>
              <span class="user-sub">{{ row.username || "System" }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column
          prop="policy_name"
          label="POLICY VIOLATED"
          min-width="200"
        >
          <template #default="{ row }">
            <span class="policy-link">{{
              row.policy?.name || "Deleted Policy"
            }}</span>
          </template>
        </el-table-column>

        <el-table-column label="ACTION" width="120">
          <template #default="{ row }">
            <el-tag
              :type="row.action === 'block' ? 'danger' : 'warning'"
              effect="dark"
              size="small"
            >
              {{ row.action.toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="DETAILS" align="right" width="100">
          <template #default="{ row }">
            <el-button link :icon="View" @click="viewDetail(row)"
              >View</el-button
            >
          </template>
        </el-table-column>
      </el-table>

      <!-- Pagination -->
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

    <!-- Detail Dialog -->
    <el-dialog v-model="detailVisible" title="Violation Details" width="600px">
      <div v-if="selectedLog" class="detail-content">
        <div class="detail-item">
          <label>File Path:</label>
          <span>{{ selectedLog.file_path || "N/A" }}</span>
        </div>
        <div class="detail-item">
          <label>Evidence (Match):</label>
          <pre class="evidence-box">{{
            selectedLog.evidence_data || "No evidence captured"
          }}</pre>
        </div>
        <div class="detail-item">
          <label>Target Channel:</label>
          <el-tag size="small">{{ selectedLog.channel }}</el-tag>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Download, View, Search, Refresh } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { exportData } from "@/utils/exporter";

const loading = ref(false);
const logs = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);

const detailVisible = ref(false);
const selectedLog = ref(null);
const searchQuery = ref("");

let searchTimer = null;
const handleSearch = () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    fetchData();
  }, 500);
};
const fetchData = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/violation-logs/", {
      params: {
        page: page.value,
        page_size: pageSize.value,
        search: searchQuery.value,
      },
    });
    logs.value = res.data.items || [];
    total.value = res.data.total || 0;
  } finally {
    loading.value = false;
  }
};

const viewDetail = (row) => {
  selectedLog.value = row;
  detailVisible.value = true;
};

const handleExport = (fmt) => {
  exportData(logs.value, `violation_logs_p${page.value}`, fmt);
};

onMounted(fetchData);
</script>

<style scoped>
.agent-info {
  display: flex;
  flex-direction: column;
}
.hostname {
  font-weight: 600;
  color: #1e293b;
}
.user-sub {
  font-size: 11px;
  color: #94a3b8;
}
.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}
.detail-item {
  margin-bottom: 15px;
}
.detail-item label {
  font-weight: bold;
  display: block;
  color: #64748b;
  margin-bottom: 5px;
}
.evidence-box {
  background: #f1f5f9;
  padding: 10px;
  border-radius: 4px;
  overflow-x: auto;
  font-family: monospace;
  font-size: 12px;
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
