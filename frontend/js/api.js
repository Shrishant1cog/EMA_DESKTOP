/**
 * Frontend API Client for Smart Universal Email Assistant
 * Communicates with the FastAPI backend URL configured via environment/config.
 */

const API_BASE_URL = "http://127.0.0.1:8000";

const ApiClient = {
  async getUser() {
    const res = await fetch(`${API_BASE_URL}/api/user`);
    return await res.json();
  },

  async getAccounts() {
    const res = await fetch(`${API_BASE_URL}/api/accounts`);
    return await res.json();
  },

  async toggleAccount(email, isMonitored) {
    const res = await fetch(`${API_BASE_URL}/api/accounts/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, is_monitored: isMonitored })
    });
    return await res.json();
  },

  async deleteAccount(email) {
    const res = await fetch(`${API_BASE_URL}/api/accounts/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email })
    });
    return await res.json();
  },

  async getSettings() {
    const res = await fetch(`${API_BASE_URL}/api/settings`);
    return await res.json();
  },

  async updateSettings(settingsObj) {
    const res = await fetch(`${API_BASE_URL}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settingsObj)
    });
    return await res.json();
  },

  async getStats() {
    const res = await fetch(`${API_BASE_URL}/api/stats`);
    return await res.json();
  },

  async getEmails(categoryFilter = "ALL", limit = 50, offset = 0) {
    const res = await fetch(`${API_BASE_URL}/api/emails?category_filter=${encodeURIComponent(categoryFilter)}&limit=${limit}&offset=${offset}`);
    return await res.json();
  },

  async toggleWorker(start = true) {
    const endpoint = start ? `${API_BASE_URL}/api/worker/start` : `${API_BASE_URL}/api/worker/stop`;
    const res = await fetch(endpoint, { method: 'POST' });
    return await res.json();
  },

  async getUpcomingCalendarEvents() {
    const res = await fetch(`${API_BASE_URL}/api/calendar/upcoming`);
    return await res.json();
  },

  async startInspection(count) {
    const res = await fetch(`${API_BASE_URL}/api/inspect/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count })
    });
    return await res.json();
  },

  async stopInspection() {
    const res = await fetch(`${API_BASE_URL}/api/inspect/stop`, { method: 'POST' });
    return await res.json();
  },

  async getInspectionStatus() {
    const res = await fetch(`${API_BASE_URL}/api/inspect/status`);
    return await res.json();
  }
};