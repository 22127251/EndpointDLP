<template>
  <div class="page-container">
    <div class="header-section">
      <h1 class="title">My Account Settings</h1>
    </div>

    <el-row :gutter="30">
      <el-col :span="12">
        <el-card shadow="never" header="Profile Information">
          <el-form label-position="top">
            <el-form-item label="Username">
              <el-input v-model="profileForm.username" />
            </el-form-item>
            <el-form-item label="Full Name">
              <el-input v-model="profileForm.full_name" />
            </el-form-item>
            <el-form-item label="Email Address">
              <el-input v-model="profileForm.email" />
            </el-form-item>
            <div style="text-align: right">
              <el-button
                type="primary"
                @click="handleUpdateProfile"
                :loading="loading"
                >Update Profile</el-button
              >
            </div>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" header="Change Password">
          <el-form label-position="top">
            <el-form-item label="New Password">
              <el-input
                v-model="passwordForm.password"
                type="password"
                show-password
              />
            </el-form-item>
            <el-form-item label="Confirm New Password">
              <el-input
                v-model="passwordForm.confirm"
                type="password"
                show-password
              />
            </el-form-item>
            <div style="text-align: right">
              <el-button
                type="danger"
                @click="handleUpdatePassword"
                :loading="pwLoading"
                >Change Password</el-button
              >
            </div>
          </el-form>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { useAuthStore } from "@/store/auth";
import apiClient from "@/api/axios";
import { ElMessage } from "element-plus";

const auth = useAuthStore();
const loading = ref(false);
const pwLoading = ref(false);

const profileForm = ref({ username: "", full_name: "", email: "" });
const passwordForm = ref({ password: "", confirm: "" });

const fetchProfile = async () => {
  const res = await apiClient.get(`/users/${auth.user.id}`);
  profileForm.value = res.data;
};

const handleUpdateProfile = async () => {
  loading.value = true;
  try {
    const res = await apiClient.patch(`/users/${auth.user.id}`, {
      full_name: profileForm.value.full_name,
      email: profileForm.value.email,
    });
    auth.setUser(res.data);
    ElMessage.success("Profile information updated");
  } finally {
    loading.value = false;
  }
};

const handleUpdatePassword = async () => {
  if (passwordForm.value.password !== passwordForm.value.confirm) {
    return ElMessage.error("Passwords do not match!");
  }
  if (passwordForm.value.password.length < 6) {
    return ElMessage.error("Password must be at least 6 characters");
  }

  pwLoading.value = true;
  console.log("Updating password for user:", passwordForm.value.password);
  try {
    await apiClient.patch(`/users/${auth.user.id}`, {
      password: passwordForm.value.password,
    });
    ElMessage.success("Password changed successfully");
  } finally {
    pwLoading.value = false;
  }
};

onMounted(fetchProfile);
</script>
