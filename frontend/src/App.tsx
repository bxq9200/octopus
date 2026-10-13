import { useEffect, useState } from 'react'
import ProjectSidebar from '@/components/sidebar/ProjectSidebar'
import FlowCanvas from '@/components/canvas/FlowCanvas'
import FloatingChat from '@/components/chat/FloatingChat'
import NodeEditorPanel from '@/components/panel/NodeEditorPanel'
import LoginPage from '@/components/login/LoginPage'
import SettingsModal from '@/components/settings/SettingsModal'
import SetupWizard from '@/components/wizard/SetupWizard'
import SeriesAssetSyncModal from '@/components/modal/SeriesAssetSyncModal'
import AssetManagerModal from '@/components/modal/AssetManagerModal'
import OutOfCreditsBanner from '@/components/OutOfCreditsBanner'
import WelcomeGuide from '@/components/WelcomeGuide'
import { settingsAPI } from '@/services/api'
import { ReactFlowProvider } from 'reactflow'

const TOKEN_KEY = 'octopus_token'

type WizardState = 'checking' | 'needed' | 'done'

function App() {
  const [authed, setAuthed] = useState(() => !!localStorage.getItem(TOKEN_KEY))
  const [wizardState, setWizardState] = useState<WizardState>('checking')

  useEffect(() => {
    if (!authed) return
    // 开发/调试：URL ?wizard=1 强制进入 setup wizard、?wizard=0 强制跳过
    const params = new URLSearchParams(window.location.search)
    const override = params.get('wizard')
    if (override === '1') { setWizardState('needed'); return }
    if (override === '0') { setWizardState('done'); return }
    settingsAPI.getStatus()
      .then((res) => setWizardState(res.data?.needs_wizard ? 'needed' : 'done'))
      .catch(() => setWizardState('done')) // 探测失败也别卡死，让用户进主界面后从侧栏改配置
  }, [authed])

  if (!authed) {
    return <LoginPage onLogin={() => setAuthed(true)} />
  }

  if (wizardState === 'checking') {
    return (
      <div className="w-screen h-screen flex items-center justify-center bg-black text-white/40 font-mono text-sm">
        <span>checking config...</span>
      </div>
    )
  }

  if (wizardState === 'needed') {
    return <SetupWizard onComplete={() => setWizardState('done')} />
  }

  return (
    <div className="w-screen h-screen flex bg-octopus-bg text-octopus-text overflow-hidden">
      <ProjectSidebar />
      {/* 聊天竖栏（替代原底部浮窗） */}
      <div className="w-[480px] shrink-0 h-full border-r border-octopus-border bg-[#14141d] relative">
        <FloatingChat />
      </div>
      {/* 画布区 */}
      <div className="flex-1 relative">
        <ReactFlowProvider>
          <FlowCanvas />
        </ReactFlowProvider>
        <NodeEditorPanel />
      </div>
      <SettingsModal />
      <SeriesAssetSyncModal />
      <AssetManagerModal />
      <OutOfCreditsBanner />
      <WelcomeGuide />
    </div>
  )
}

export default App
