<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">Agent Logs</h1>
        <p class="subtitle">View event logs and agent diagnostics from all endpoints.</p>
      </div>
    </div>

    <el-card shadow="never">
      <div class="toolbar">
        <el-select
          v-model="selectedAgent"
          placeholder="Select Agent"
          clearable
          style="width: 280px"
          @change="handleFilterChange"
        >
          <el-option
            v-for="a in allAgents"
            :key="a.id"
            :label="a.hostname"
            :value="a.id"
          />
        </el-select>

        <el-select
          v-model="selectedType"
          placeholder="Log Type"
          clearable
          style="width: 160px"
          @change="handleFilterChange"
        >
          <el-option label="Events" value="events" />
          <el-option label="Agent Log" value="agent_log" />
        </el-select>

        <el-button :icon="Refresh" @click="fetchLogs">Reload</el-button>
      </div>

      <el-table :data="logs" v-loading="loading" style="width: 100%">
        <el-table-column label="AGENT" min-width="160">
          <template #default="{ row }">
            <span class="mono">{{ getAgentHostname(row.agent_id) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="TYPE" width="120">
          <template #default="{ row }">
            <el-tag
              :type="row.log_type === 'events' ? 'danger' : 'info'"
              size="small"
              effect="dark"
            >
              {{ row.log_type.toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="TIME" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="CONTENT" min-width="400">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">
              View Content
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          :total="total"
          @size-change="fetchLogs"
          @current-change="fetchLogs"
        />
      </div>
    </el-card>

    <!-- Detail Dialog -->
    <el-dialog
      v-model="detailVisible"
      :title="`${detailRow?.log_type === 'events' ? 'Events' : 'Agent Log'} — ${getAgentHostname(detailRow?.agent_id)}`"
      width="800px"
    >
      <pre class="log-content">{{ detailRow?.content }}</pre>
      <template #footer>
        <el-button @click="detailVisible = false">Close</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Refresh } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";

const loading = ref(false);
const logs = ref([]);
const allAgents = ref([]);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const selectedAgent = ref("");
const selectedType = ref("");

const detailVisible = ref(false);
const detailRow = ref(null);

const fetchLogs = async () => {
  loading.value = true;
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
    };
    if (selectedType.value) params.log_type = selectedType.value;

    let url;
    if (selectedAgent.value) {
      url = `/agents/${selectedAgent.value}/logs`;
    } else {
      // Fetch from all agents
      url = "/agents/";
    }

    if (selectedAgent.value) {
      const res = await apiClient.get(url, { params });
      logs.value = res.data.items || [];
      total.value = res.data.total || 0;
    } else {
      // List all agents, then fetch logs for each (last page)
      const aRes = await apiClient.get("/agents/", { params: { page: 1, page_size: 100 } });
      allAgents.value = aRes.data.items || [];

      // For no agent selected: fetch logs from ALL agents
      const allLogs = [];
      for (const agent of allAgents.value) {
        try {
          const lRes = await apiClient.get(`/agents/${agent.id}/logs`, { params });
          const items = lRes.data.items || [];
          allLogs.push(...items);
        } catch { /* skip */ }
      }
      allLogs.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
      total.value = allLogs.length;
      logs.value = allLogs.slice((page.value - 1) * pageSize.value, page.value * pageSize.value);
    }
  } finally {
    loading.value = false;
  }
};

const fetchAgents = async () => {
  try {
    const res = await apiClient.get("/agents/", { params: { page: 1, page_size: 100 } });
    allAgents.value = res.data.items || [];
  } catch { /* skip */ }
};

const handleFilterChange = () => {
  page.value = 1;
  fetchLogs();
};

const getAgentHostname = (agentId) => {
  const agent = allAgents.value.find((a) => a.id === agentId);
  return agent?.hostname || agentId || "—";
};

const formatTime = (dt) => {
  if (!dt) return "—";
  return new Date(dt).toLocaleString();
};

const openDetail = (row) => {
  detailRow.value = row;
  detailVisible.value = true;
};

onMounted(() => {
  fetchAgents();
  fetchLogs();
});
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
}
.mono {
  font-family: monospace;
  font-size: 13px;
}
.log-content {
  background: #1e293b;
  color: #e2e8f0;
  padding: 16px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  max-height: 500px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.pagination-container {
  margin-top: 24px;
  display: flex;
  justify-content: flex-end;
  padding-bottom: 8px;
}
</style>
