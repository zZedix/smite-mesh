import { useEffect, useState } from 'react'
import { Plus, Trash2, Play, RotateCw, Activity, Network } from 'lucide-react'
import api from '../api/client'
import React from 'react'

interface Mesh {
  id: string
  name: string
  topology: string
  overlay_subnet: string
  mtu: number
  status: string
  created_at: string
  updated_at: string
  mesh_config: Record<string, any>
}

interface Node {
  id: string
  name: string
  metadata: Record<string, any>
}

interface MeshStatus {
  mesh_id: string
  mesh_name: string
  status: string
  nodes: Record<string, {
    active?: boolean
    interface?: string
    lan_subnet?: string
    node_name?: string
    overlay_ip?: string
    peers?: Array<{
      public_key: string
      endpoint?: string
      allowed_ips?: string
      last_handshake?: string
      connected?: boolean
    }>
    error?: string
  }>
}

const Mesh = () => {
  const [meshes, setMeshes] = useState<Mesh[]>([])
  const [nodes, setNodes] = useState<Node[]>([])
  const [loading, setLoading] = useState(true)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [selectedMesh, setSelectedMesh] = useState<string | null>(null)
  const [meshStatus, setMeshStatus] = useState<MeshStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [applying, setApplying] = useState<string | null>(null)
  const [rotating, setRotating] = useState<string | null>(null)

  useEffect(() => {
    fetchMeshes()
    fetchNodes()
  }, [])

  const fetchMeshes = async () => {
    try {
      const response = await api.get('/mesh')
      setMeshes(response.data)
    } catch (error) {
      console.error('Failed to fetch meshes:', error)
    } finally {
      setLoading(false)
    }
  }

  const fetchNodes = async () => {
    try {
      const response = await api.get('/nodes')
      const allNodes = response.data.filter((node: Node) => 
        node.metadata?.role === 'iran' || node.metadata?.role === 'foreign' || !node.metadata?.role
      )
      setNodes(allNodes)
    } catch (error) {
      console.error('Failed to fetch nodes:', error)
    }
  }

  const fetchMeshStatus = async (meshId: string) => {
    setStatusLoading(true)
    try {
      const response = await api.get(`/mesh/${meshId}/status`)
      setMeshStatus(response.data)
      setSelectedMesh(meshId)
    } catch (error) {
      console.error('Failed to fetch mesh status:', error)
      alert('Failed to fetch mesh status')
    } finally {
      setStatusLoading(false)
    }
  }

  const handleApply = async (meshId: string) => {
    if (!confirm('Apply mesh configuration to all nodes?')) return
    
    setApplying(meshId)
    try {
      await api.post(`/mesh/${meshId}/apply`)
      alert('Mesh applied successfully')
      await fetchMeshes()
      if (selectedMesh === meshId) {
        await fetchMeshStatus(meshId)
      }
    } catch (error: any) {
      console.error('Failed to apply mesh:', error)
      alert(error.response?.data?.detail || 'Failed to apply mesh')
    } finally {
      setApplying(null)
    }
  }

  const handleRotateKeys = async (meshId: string) => {
    if (!confirm('Rotate WireGuard keys? You will need to re-apply the mesh after rotation.')) return
    
    setRotating(meshId)
    try {
      await api.post(`/mesh/${meshId}/rotate-keys`)
      alert('Keys rotated successfully. Please re-apply the mesh.')
      await fetchMeshes()
    } catch (error: any) {
      console.error('Failed to rotate keys:', error)
      alert(error.response?.data?.detail || 'Failed to rotate keys')
    } finally {
      setRotating(null)
    }
  }

  const handleDelete = async (meshId: string) => {
    if (!confirm('Are you sure you want to delete this mesh? This will remove all WireGuard configurations from nodes.')) return
    
    try {
      await api.delete(`/mesh/${meshId}`)
      alert('Mesh deleted successfully')
      await fetchMeshes()
      if (selectedMesh === meshId) {
        setSelectedMesh(null)
        setMeshStatus(null)
      }
    } catch (error: any) {
      console.error('Failed to delete mesh:', error)
      alert(error.response?.data?.detail || 'Failed to delete mesh')
    }
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'active':
        return 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-200'
      case 'pending':
        return 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-200'
      case 'error':
        return 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200'
      default:
        return 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200'
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="inline-block animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mb-4"></div>
          <p className="text-gray-500 dark:text-gray-400">Loading meshes...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-white flex items-center gap-2">
            <Network className="w-8 h-8" />
            WireGuard Mesh
          </h1>
          <p className="text-gray-500 dark:text-gray-400 mt-1">
            Site-to-site VPN mesh networks over FRP
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
        >
          <Plus size={20} />
          Create Mesh
        </button>
      </div>

      {meshes.length === 0 ? (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-12 text-center">
          <Network className="w-16 h-16 mx-auto text-gray-400 dark:text-gray-500 mb-4" />
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">No meshes yet</h3>
          <p className="text-gray-500 dark:text-gray-400 mb-4">
            Create your first WireGuard mesh to connect multiple locations
          </p>
          <button
            onClick={() => setShowCreateModal(true)}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            Create Mesh
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {meshes.map((mesh) => (
            <div
              key={mesh.id}
              className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 hover:shadow-lg transition-shadow"
            >
              <div className="flex justify-between items-start mb-4">
                <div>
                  <h3 className="text-xl font-semibold text-gray-900 dark:text-white">{mesh.name}</h3>
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                    {mesh.topology} • {mesh.mesh_config?.transport === 'both' ? 'TCP+UDP' : (mesh.mesh_config?.transport?.toUpperCase() || 'UDP')} • {mesh.overlay_subnet} • MTU: {mesh.mtu}
                  </p>
                </div>
                <span className={`px-3 py-1 rounded-full text-xs font-medium ${getStatusColor(mesh.status)}`}>
                  {mesh.status}
                </span>
              </div>

              <div className="space-y-2 mb-4">
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  <span className="font-medium">Nodes:</span>{' '}
                  {mesh.mesh_config?.nodes ? Object.keys(mesh.mesh_config.nodes).length : 0}
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  <span className="font-medium">Created:</span>{' '}
                  {new Date(mesh.created_at).toLocaleDateString()}
                </div>
              </div>

              <div className="flex gap-2 flex-wrap">
                <button
                  onClick={() => fetchMeshStatus(mesh.id)}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                >
                  <Activity size={16} />
                  Status
                </button>
                <button
                  onClick={() => handleApply(mesh.id)}
                  disabled={applying === mesh.id}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded hover:bg-blue-200 dark:hover:bg-blue-900/50 transition-colors disabled:opacity-50"
                >
                  <Play size={16} />
                  {applying === mesh.id ? 'Applying...' : 'Apply'}
                </button>
                <button
                  onClick={() => handleRotateKeys(mesh.id)}
                  disabled={rotating === mesh.id}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300 rounded hover:bg-yellow-200 dark:hover:bg-yellow-900/50 transition-colors disabled:opacity-50"
                >
                  <RotateCw size={16} />
                  {rotating === mesh.id ? 'Rotating...' : 'Rotate Keys'}
                </button>
                <button
                  onClick={() => handleDelete(mesh.id)}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 rounded hover:bg-red-200 dark:hover:bg-red-900/50 transition-colors"
                >
                  <Trash2 size={16} />
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {selectedMesh && meshStatus && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
              Mesh Status: {meshStatus.mesh_name}
            </h2>
            <button
              onClick={() => {
                setSelectedMesh(null)
                setMeshStatus(null)
              }}
              className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            >
              ×
            </button>
          </div>

          {statusLoading ? (
            <div className="text-center py-8">
              <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
            </div>
          ) : (
            <div className="space-y-4">
              {Object.entries(meshStatus.nodes).map(([nodeId, nodeStatus]) => {
                const node = nodes.find(n => n.id === nodeId)
                return (
                  <div key={nodeId} className="border dark:border-gray-700 rounded-lg p-4">
                    <div className="flex justify-between items-center mb-2">
                      <h3 className="font-semibold text-gray-900 dark:text-white">
                        {nodeStatus.node_name || node?.name || nodeId}
                      </h3>
                      <span className={`px-2 py-1 rounded text-xs ${nodeStatus.active ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-200' : 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200'}`}>
                        {nodeStatus.active ? 'Active' : 'Inactive'}
                      </span>
                    </div>
                    {nodeStatus.lan_subnet && (
                      <div className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                        LAN Subnet: <code className="text-blue-600 dark:text-blue-400 font-mono">{nodeStatus.lan_subnet}</code>
                      </div>
                    )}
                    {nodeStatus.interface && (
                      <div className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                        Interface: {nodeStatus.interface}
                      </div>
                    )}
                    {nodeStatus.peers && nodeStatus.peers.length > 0 && (
                      <div className="mt-3">
                        <div className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Peers:</div>
                        <div className="space-y-2">
                          {nodeStatus.peers.map((peer, idx) => (
                            <div key={idx} className="text-xs bg-gray-50 dark:bg-gray-900 rounded p-2">
                              <div className="flex justify-between items-center">
                                <span className="font-mono text-gray-600 dark:text-gray-400">
                                  {peer.public_key.substring(0, 20)}...
                                </span>
                                {peer.connected !== undefined && (
                                  <span className={`px-2 py-0.5 rounded ${peer.connected ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-200' : 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200'}`}>
                                    {peer.connected ? 'Connected' : 'Disconnected'}
                                  </span>
                                )}
                              </div>
                              {peer.endpoint && (
                                <div className="text-gray-500 dark:text-gray-500 mt-1">
                                  Endpoint: {peer.endpoint}
                                </div>
                              )}
                              {peer.last_handshake && (
                                <div className="text-gray-500 dark:text-gray-500 mt-1">
                                  Last handshake: {peer.last_handshake}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {nodeStatus.error && (
                      <div className="text-sm text-red-600 dark:text-red-400 mt-2">
                        Error: {nodeStatus.error}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {showCreateModal && (
        <CreateMeshModal
          nodes={nodes}
          onClose={() => setShowCreateModal(false)}
          onSuccess={() => {
            setShowCreateModal(false)
            fetchMeshes()
          }}
        />
      )}
    </div>
  )
}

interface CreateMeshModalProps {
  nodes: Node[]
  onClose: () => void
  onSuccess: () => void
}

const CreateMeshModal = ({ nodes, onClose, onSuccess }: CreateMeshModalProps) => {
  const [name, setName] = useState('')
  const [selectedNodes, setSelectedNodes] = useState<string[]>([])
  const [lanSubnets, setLanSubnets] = useState<Record<string, string>>({})
  const [overlaySubnet, setOverlaySubnet] = useState('')
  const [topology, setTopology] = useState<'full-mesh' | 'hub-spoke'>('full-mesh')
  const [transport, setTransport] = useState<'tcp' | 'udp' | 'both'>('both')
  const [mtu, setMtu] = useState('1280')
  const [wireguardPort, setWireguardPort] = useState('')
  const [loading, setLoading] = useState(false)
  const [poolStatus, setPoolStatus] = useState<any>(null)

  useEffect(() => {
    const fetchPool = async () => {
      try {
        const response = await api.get('/overlay/status')
        setPoolStatus(response.data)
        if (response.data.pool_exists && !overlaySubnet) {
          setOverlaySubnet(response.data.cidr || '')
        }
      } catch (error) {
        console.error('Failed to fetch pool status:', error)
      }
    }
    fetchPool()
  }, [])

  const handleNodeToggle = (nodeId: string) => {
    if (selectedNodes.includes(nodeId)) {
      setSelectedNodes(selectedNodes.filter(id => id !== nodeId))
      const newSubnets = { ...lanSubnets }
      delete newSubnets[nodeId]
      setLanSubnets(newSubnets)
    } else {
      setSelectedNodes([...selectedNodes, nodeId])
      setLanSubnets({ ...lanSubnets, [nodeId]: '' })
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    
    if (selectedNodes.length < 2) {
      alert('Please select at least 2 nodes')
      return
    }

    setLoading(true)
    try {
      await api.post('/mesh/create', {
        name,
        node_ids: selectedNodes,
        lan_subnets: lanSubnets,
        overlay_subnet: overlaySubnet || undefined,
        topology,
        transport,
        mtu: parseInt(mtu) || 1280,
        wireguard_port: wireguardPort ? parseInt(wireguardPort) : undefined
      })
      onSuccess()
    } catch (error: any) {
      console.error('Failed to create mesh:', error)
      alert(error.response?.data?.detail || 'Failed to create mesh')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        <div className="p-6">
          <div className="flex justify-between items-center mb-6">
            <h2 className="text-2xl font-bold text-gray-900 dark:text-white">Create WireGuard Mesh</h2>
            <button
              onClick={onClose}
              className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            >
              ×
            </button>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Mesh Name
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                placeholder="Office Mesh"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Select Nodes & Servers (at least 2)
              </label>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                Both Master nodes and Slave servers can participate in the mesh
              </p>
              <div className="border border-gray-300 dark:border-gray-600 rounded-lg p-3 max-h-48 overflow-y-auto">
                {nodes.length === 0 ? (
                  <p className="text-sm text-gray-500 dark:text-gray-400">No nodes available</p>
                ) : (
                  nodes.map((node) => {
                    const nodeRole = node.metadata?.role || 'iran'
                    const roleLabel = nodeRole === 'foreign' ? 'Slave' : 'Master'
                    return (
                      <div key={node.id} className="flex items-center gap-3 mb-2">
                        <input
                          type="checkbox"
                          checked={selectedNodes.includes(node.id)}
                          onChange={() => handleNodeToggle(node.id)}
                          className="w-4 h-4"
                        />
                        <label className="flex-1 text-sm text-gray-700 dark:text-gray-300">
                          <span>{node.name}</span>
                          <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">
                            ({roleLabel})
                          </span>
                        </label>
                        {selectedNodes.includes(node.id) && (
                          <input
                            type="text"
                            value={lanSubnets[node.id] || ''}
                            onChange={(e) => setLanSubnets({ ...lanSubnets, [node.id]: e.target.value })}
                            className="flex-1 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700 dark:text-white"
                            placeholder="192.168.10.0/24 (optional)"
                          />
                        )}
                      </div>
                    )
                  })
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Overlay Subnet
              </label>
              <input
                type="text"
                value={overlaySubnet}
                onChange={(e) => setOverlaySubnet(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                placeholder={poolStatus?.cidr || "Auto (from IPAM pool)"}
                disabled={!!poolStatus?.cidr}
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                {poolStatus?.pool_exists 
                  ? `Using IPAM pool: ${poolStatus.cidr}. Nodes will use their assigned overlay IPs.`
                  : "No IPAM pool configured. Please create an overlay pool first."}
              </p>
            </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  MTU
                </label>
                <input
                  type="number"
                  value={mtu}
                  onChange={(e) => setMtu(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  min="1280"
                  max="1500"
                  required
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Topology
                </label>
                <select
                  value={topology}
                  onChange={(e) => setTopology(e.target.value as 'full-mesh' | 'hub-spoke')}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                >
                  <option value="full-mesh">Full Mesh (all nodes connect to each other)</option>
                  <option value="hub-spoke">Hub-Spoke (all nodes connect to first node)</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  FRP Transport
                </label>
                <select
                  value={transport}
                  onChange={(e) => setTransport(e.target.value as 'tcp' | 'udp' | 'both')}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                >
                  <option value="both">Both TCP & UDP (recommended - redundancy)</option>
                  <option value="udp">UDP only (lower latency, better for real-time)</option>
                  <option value="tcp">TCP only (more reliable, better for restrictive networks)</option>
                </select>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {transport === 'both' 
                    ? 'Creates both TCP and UDP tunnels for redundancy and better connectivity'
                    : 'Transport protocol for FRP tunnels'}
                </p>
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                WireGuard Port (Optional)
              </label>
              <input
                type="number"
                value={wireguardPort}
                onChange={(e) => setWireguardPort(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                placeholder="Auto (random 17000-17999)"
                min="1"
                max="65535"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Custom port for WireGuard local_port and remote_port. Leave empty for random port. Both ports will use the same value.
              </p>
            </div>

            <div className="flex justify-end gap-3 pt-4">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={loading || selectedNodes.length < 2}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
              >
                {loading ? 'Creating...' : 'Create Mesh'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}

export default Mesh

