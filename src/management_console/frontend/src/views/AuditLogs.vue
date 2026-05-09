<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Audit Logs</h1>
        <p class="subtitle">
          Detailed history of actions performed on the system.
        </p>
      </div>
      <div class="header-actions">
        <el-button :icon="Refresh" @click="fetchData">Refresh</el-button>
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
        <el-table-column label="TIMESTAMP" width="180">
          <template #default="{ row }">
            <div class="time-cell">{{ formatTime(row.created_at) }}</div>
          </template>
        </el-table-column>

        <el-table-column prop="username" label="ADMIN USER" width="150">
          <template #default="{ row }">
            <span class="user-bold">{{ row.username }}</span>
          </template>
        </el-table-column>

        <el-table-column label="ACTION" width="120">
          <template #default="{ row }">
            <el-tag
              :type="getActionType(row.action)"
              size="small"
              effect="dark"
            >
              {{ row.action.toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="TARGET" width="150">
          <template #default="{ row }">
            <el-tag type="info" size="small" effect="plain">
              {{ row.target_type.toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column
          prop="description"
          label="DESCRIPTION"
          min-width="250"
          show-overflow-tooltip
        />

        <el-table-column label="TARGET ID" width="120">
          <template #default="{ row }">
            <span class="id-text" v-if="row.target_id"
              >{{ row.target_id.slice(0, 8) }}...</span
            >
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="fetchData"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Refresh, Search } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";

const loading = ref(false);
const logs = ref([]);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);

const fetchData = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/audit-logs/", {
      params: { page: page.value, page_size: pageSize.value },
    });
    logs.value = res.data.items;
    total.value = res.data.total || 0;
  } finally {
    loading.value = false;
  }
};

const getActionType = (action) => {
  const a = action.toLowerCase();
  if (a.includes("create")) return "success";
  if (a.includes("delete")) return "danger";
  if (a.includes("update") || a.includes("patch")) return "warning";
  if (a.includes("login")) return "info";
  return "info";
};

const formatTime = (timeStr) => {
  return new Date(timeStr).toLocaleString("en-GB");
};

onMounted(fetchData);
</script>

<style scoped>
.time-cell {
  font-family: monospace;
  font-size: 13px;
  color: #64748b;
}
.user-bold {
  font-weight: 700;
  color: #1e293b;
}
.id-text {
  font-family: monospace;
  font-size: 11px;
  color: #94a3b8;
}
.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

:deep(.el-tag--dark.el-tag--success) {
  background-color: #10b981;
  border-color: #10b981;
}
:deep(.el-tag--dark.el-tag--danger) {
  background-color: #ef4444;
  border-color: #ef4444;
}
:deep(.el-tag--dark.el-tag--warning) {
  background-color: #f59e0b;
  border-color: #f59e0b;
}
:deep(.el-tag--dark.el-tag--info) {
  background-color: #3b82f6;
  border-color: #3b82f6;
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
