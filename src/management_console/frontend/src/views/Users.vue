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
            <el-option label="Admin" value="admin" />
            <el-option label="User" value="user" />
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
import { Plus, Edit, Delete } from "@element-plus/icons-vue";
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

const fetchUsers = async () => {
  loading.value = true;
  try {
    const res = await apiClient.get("/users/");
    users.value = res.data.items || res.data;
  } finally {
    loading.value = false;
  }
};

const openCreateDialog = () => {
  isEdit.value = false;
  userForm.value = {
    username: "",
    password: "",
    full_name: "",
    email: "",
    role: "user",
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
    fetchUsers();
  } catch (e) {
    ElMessage.error("Action failed");
  }
};

const handleDelete = (id) => {
  ElMessageBox.confirm("Delete this user?").then(async () => {
    await apiClient.delete(`/users/${id}`);
    fetchUsers();
  });
};

onMounted(fetchUsers);
</script>
