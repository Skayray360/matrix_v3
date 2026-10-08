// Validation-only guard: never loaded by the application or shipped build.
const net = require('node:net');
const dgram = require('node:dgram');
const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const normalized = Array.isArray(args[0]) ? args[0] : args;
  const options = typeof normalized[0] === 'object' ? normalized[0] : {};
  const host = options.host || (typeof normalized[1] === 'string' ? normalized[1] : 'localhost');
  // Vitest's module runner uses local IPC/loopback. Tests mock the API.
  if (!options.path && !['localhost', '127.0.0.1', '::1'].includes(host)) {
    throw new Error('External network disabled for synthetic frontend validation');
  }
  return originalConnect.apply(this, args);
};
dgram.Socket.prototype.send = function () { throw new Error('Network disabled for synthetic frontend validation'); };
