<template>
  <div class="page-container">
    <div class="header-section">
      <div>
        <h1 class="title">User Management</h1>
        <p class="subtitle">Manage administrative accounts and permissions.</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreateDialog"
        >Create User</el-button
      >
    </div>

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
      <el-table :data="users" v-loading="loading">
        <el-table-column prop="username" label="USERNAME" font-weight="bold" />
        <el-table-column prop="full_name" label="FULL NAME" />
        <el-table-column prop="email" label="EMAIL" />
        <el-table-column label="ROLE">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'danger' : 'info'">{{
              row.role.toUpperCase()
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="ACTIONS" width="150" align="right">
          <template #default="{ row }">
            <el-button link :icon="Edit" @click="openEditDialog(row)" />
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

    <!-- USER DIALOG (Create/Edit) -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? 'Edit User' : 'Create User'"
      width="450px"
    >
      <el-form :model="userForm" label-position="top">
        <el-form-item label="Username" required>
          <el-input v-model="userForm.username" :disabled="isEdit" />
        </el-form-item>
        <el-form-item label="Password" v-if="!isEdit" required>
          <el-input v-model="userForm.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="Full Name">
          <el-input v-model="userForm.full_name" />
        </el-form-item>
        <el-form-item label="Email">
          <el-input v-model="userForm.email" />
        </el-form-item>
        <el-form-item label="Role">
          <el-select v-model="userForm.role" style="width: 100%">
            <el-option label="admin" value="admin" />
            <el-option label="viewer" value="viewer" />
            <el-option label="operator" value="operator" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">Cancel</el-button>
        <el-button type="primary" @click="submitForm">Save</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Plus, Edit, Delete, Search, Refresh } from "@element-plus/icons-vue";
import apiClient from "@/api/axios";
import { ElMessage, ElMessageBox } from "element-plus";

const users = ref([]);
const loading = ref(false);
const dialogVisible = ref(false);
const isEdit = ref(false);
const userForm = ref({
  username: "",
  password: "",
  full_name: "",
  email: "",
  role: "user",
});

const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const searchQuery = ref("");

let searchTimer = null;

const fetchData = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/users/", {
      params: {
        page: page.value,
        page_size: pageSize.value,
        search: searchQuery.value,
      },
    });
    users.value = res.data.items || res.data;
    total.value = res.data.total || 0;
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

const openCreateDialog = () => {
  isEdit.value = false;
  userForm.value = {
    username: "",
    password: "",
    full_name: "",
    email: "",
    role: "viewer",
  };
  dialogVisible.value = true;
};

const openEditDialog = (row) => {
  isEdit.value = true;
  userForm.value = { ...row };
  dialogVisible.value = true;
};

const submitForm = async () => {
  try {
    if (isEdit.value) {
      await apiClient.patch(`/users/${userForm.value.id}`, userForm.value);
      ElMessage.success("User updated");
    } else {
      await apiClient.post("/users/", userForm.value);
      ElMessage.success("User created");
    }
    dialogVisible.value = false;
    fetchData();
  } catch (e) {
    ElMessage.error("Action failed");
  }
};

const handleDelete = (id) => {
  ElMessageBox.confirm("Delete this user?").then(async () => {
    await apiClient.delete(`/users/${id}`);
    fetchData();
  });
};

onMounted(fetchData);
</script>

<style scoped>
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
