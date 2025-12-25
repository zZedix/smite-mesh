import { useEffect, useState } from 'react'
import { Network, Plus, AlertTriangle, CheckCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import api from '../api/client'

interface PoolStatus {
  pool_exists: boolean
  cidr?: string
  description?: string
  total_ips: number
  assigned_ips: number
  available_ips: number
  utilization: number
  exhausted: boolean
  error?: string
}

interface Assignment {
  node_id: string
  node_name: string
  overlay_ip: string
  interface_name: string
  assigned_at: string | null
}

const Overlay = () => {
  const { t } = useTranslation()
  const [poolStatus, setPoolStatus] = useState<PoolStatus | null>(null)
  const [assignments, setAssignments] = useState<Assignment[]>([])
  const [loading, setLoading] = useState(true)
  const [showPoolModal, setShowPoolModal] = useState(false)
  const [cidr, setCidr] = useState('10.250.0.0/24')
  const [description, setDescription] = useState('')

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 10000)
    return () => clearInterval(interval)
  }, [])

  const fetchData = async () => {
    try {
      const [statusRes, assignmentsRes] = await Promise.all([
        api.get('/overlay/status'),
        api.get('/overlay/assignments')
      ])
      setPoolStatus(statusRes.data)
      setAssignments(assignmentsRes.data)
    } catch (error) {
      console.error('Failed to fetch overlay data:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleCreatePool = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await api.post('/overlay/pool', { cidr, description })
      setShowPoolModal(false)
      fetchData()
    } catch (error: any) {
      console.error('Failed to create pool:', error)
      alert(error.response?.data?.detail || 'Failed to create pool')
    }
  }

  const handleDeletePool = async () => {
    if (!confirm('Are you sure you want to delete the overlay pool? This will remove all IP assignments.')) {
      return
    }
    try {
      await api.delete('/overlay/pool')
      fetchData()
    } catch (error: any) {
      console.error('Failed to delete pool:', error)
      alert(error.response?.data?.detail || 'Failed to delete pool')
    }
  }

  const handleSyncIPs = async () => {
    try {
      const response = await api.post('/overlay/sync')
      alert(`Synced ${response.data.synced} nodes. ${response.data.errors.length > 0 ? 'Errors: ' + response.data.errors.join(', ') : ''}`)
      fetchData()
    } catch (error: any) {
      console.error('Failed to sync IPs:', error)
      alert(error.response?.data?.detail || 'Failed to sync IPs')
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <div className="inline-block animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mb-4"></div>
          <p className="text-gray-500 dark:text-gray-400">Loading overlay status...</p>
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
            {t('overlay.title')}
          </h1>
          <p className="text-gray-500 dark:text-gray-400 mt-1">
            {t('overlay.subtitle')}
          </p>
        </div>
        {(!poolStatus?.pool_exists) && (
          <button
            onClick={() => setShowPoolModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            <Plus size={20} />
            Create Pool
          </button>
        )}
      </div>

      {poolStatus && poolStatus.pool_exists ? (
        <>
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <div className="flex justify-between items-start mb-4">
              <div>
                <h2 className="text-xl font-semibold text-gray-900 dark:text-white">Pool Status</h2>
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                  {poolStatus.cidr} {poolStatus.description && `• ${poolStatus.description}`}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {poolStatus.exhausted && (
                  <div className="flex items-center gap-2 px-3 py-1 bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200 rounded-full text-sm">
                    <AlertTriangle size={16} />
                    Pool Exhausted
                  </div>
                )}
                <button
                  onClick={handleDeletePool}
                  className="px-3 py-1 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors"
                >
                  Delete Pool
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4">
              <div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Total IPs</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{poolStatus.total_ips}</div>
              </div>
              <div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Assigned</div>
                <div className="text-2xl font-bold text-blue-600 dark:text-blue-400">{poolStatus.assigned_ips}</div>
              </div>
              <div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Available</div>
                <div className={`text-2xl font-bold ${poolStatus.available_ips === 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
                  {poolStatus.available_ips}
                </div>
              </div>
              <div>
                <div className="text-sm text-gray-500 dark:text-gray-400">Utilization</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-white">{poolStatus.utilization}%</div>
              </div>
            </div>

            <div className="mt-4">
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                <div
                  className={`h-2 rounded-full transition-all ${
                    poolStatus.utilization >= 90 ? 'bg-red-600' : poolStatus.utilization >= 70 ? 'bg-yellow-600' : 'bg-blue-600'
                  }`}
                  style={{ width: `${Math.min(poolStatus.utilization, 100)}%` }}
                ></div>
              </div>
            </div>
          </div>

          <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white">IP Assignments</h2>
              <button
                onClick={handleSyncIPs}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                Sync IPs to All Nodes
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-gray-50 dark:bg-gray-700/50">
                  <tr>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">
                      Node Name
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">
                      Overlay IP
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">
                      Interface
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">
                      Assigned At
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                  {assignments.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="px-6 py-8 text-center text-gray-500 dark:text-gray-400">
                        No IP assignments yet
                      </td>
                    </tr>
                  ) : (
                    assignments.map((assignment) => (
                      <tr key={assignment.node_id} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                        <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-white">
                          {assignment.node_name}
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <code className="text-sm text-blue-600 dark:text-blue-400 font-mono">
                            {assignment.overlay_ip}
                          </code>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          {assignment.interface_name}
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                          {assignment.assigned_at ? new Date(assignment.assigned_at).toLocaleString() : 'N/A'}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-12 text-center">
          <Network className="w-16 h-16 mx-auto text-gray-400 dark:text-gray-500 mb-4" />
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">No Overlay Pool Configured</h3>
          <p className="text-gray-500 dark:text-gray-400 mb-4">
            Create an overlay IP pool to start assigning IPs to nodes
          </p>
          <button
            onClick={() => setShowPoolModal(true)}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            Create Pool
          </button>
        </div>
      )}

      {showPoolModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full">
            <div className="p-6">
              <div className="flex justify-between items-center mb-6">
                <h2 className="text-2xl font-bold text-gray-900 dark:text-white">Create Overlay Pool</h2>
                <button
                  onClick={() => setShowPoolModal(false)}
                  className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                >
                  ×
                </button>
              </div>

              <form onSubmit={handleCreatePool} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    CIDR
                  </label>
                  <input
                    type="text"
                    value={cidr}
                    onChange={(e) => setCidr(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="10.250.0.0/24"
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Example: 10.250.0.0/24 (254 usable IPs)
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Description (optional)
                  </label>
                  <input
                    type="text"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="Main overlay network"
                  />
                </div>

                <div className="flex justify-end gap-3 pt-4">
                  <button
                    type="button"
                    onClick={() => setShowPoolModal(false)}
                    className="px-4 py-2 text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                  >
                    Create Pool
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default Overlay

