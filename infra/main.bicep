// Employee PTO (RAG + Databricks on AWS) – Azure infrastructure.
// Secrets live in Key Vault; Container Apps read them through Key Vault references with
// user-assigned managed identities. PostgreSQL uses Entra ID auth only (no passwords).
// All outbound traffic leaves through a NAT gateway with one static IP, so the Databricks
// on AWS workspace IP access list and the PostgreSQL firewall can allow exactly that IP.
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param prefix string = 'ptorag'

@description('Image tag pushed to the ACR created here (see README).')
param imageTag string = 'latest'
@description('Set false on the first deployment (before images are pushed to ACR).')
param deployApps bool = true
param allowedOrigins string = ''

// PostgreSQL Entra admin (a user or group that runs schema.sql / roles.sql)
param pgEntraAdminObjectId string
param pgEntraAdminName string
@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param pgEntraAdminType string = 'User'
@description('Optional: your public IP, to run schema.sql and local dev against PostgreSQL.')
param pgClientIp string = ''

// Field-level PII encryption key: base64 of 32 random bytes (see README). No default:
// a generated value would change on every deployment and make stored PII unreadable.
@secure()
param piiEncryptionKey string

// Azure OpenAI deployments in the Foundry resource (Entra ID auth)
param azureOpenAIEndpoint string      // https://<resource>.openai.azure.com
param chatDeployment string = 'gpt-4.1'
param embeddingDeployment string = 'text-embedding-3-small'

// Databricks on AWS (OAuth M2M service principal)
param databricksAwsHost string        // dbc-xxxx.cloud.databricks.com
param databricksAwsHttpPath string    // /sql/1.0/warehouses/xxxx
param databricksAwsClientId string
@secure()
param databricksAwsClientSecret string

// Website sign-in (Container Apps built-in auth, Entra ID). Empty = /pto stays disabled (503).
param entraAuthClientId string = ''
@secure()
param entraAuthClientSecret string = ''

var suffix = uniqueString(resourceGroup().id)
var kvSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538f'
var blobReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var authEnabled = !empty(entraAuthClientId)

// ---------- Identities: API (read-only) and pipelines (writer) ----------
resource apiId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
}

resource pipelinesId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-pipelines-id'
  location: location
}

// ---------- Network: static egress IP ----------
resource egressIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${prefix}-egress-ip'
  location: location
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource nat 'Microsoft.Network/natGateways@2023-11-01' = {
  name: '${prefix}-nat'
  location: location
  sku: { name: 'Standard' }
  properties: { publicIpAddresses: [ { id: egressIp.id } ], idleTimeoutInMinutes: 10 }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.40.0.0/16' ] }
    subnets: [ {
      name: 'aca'
      properties: {
        addressPrefix: '10.40.0.0/23'
        natGateway: { id: nat.id }
        delegations: [ { name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } } ]
      }
    } ]
  }
}

// ---------- Key Vault ----------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}-kv-${take(suffix, 6)}'
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
  }
}

resource kvRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: kv
  name: guid(kv.id, i == 0 ? apiId.id : pipelinesId.id, kvSecretsUser)
  properties: {
    principalId: i == 0 ? apiId.properties.principalId : pipelinesId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUser)
  }
}]

var secrets = {
  'pii-encryption-key': piiEncryptionKey
  'databricks-aws-client-secret': databricksAwsClientSecret
}

resource kvSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for s in items(secrets): {
  parent: kv
  name: s.key
  properties: { value: s.value }
}]

resource authSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (authEnabled) {
  parent: kv
  name: 'entra-auth-client-secret'
  properties: { value: entraAuthClientSecret }
}

func kvRef(vaultUri string, name string, identityId string) object => {
  name: name
  keyVaultUrl: '${vaultUri}secrets/${name}'
  identity: identityId
}

// ---------- PostgreSQL Flexible Server (pgvector) ----------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${prefix}-pg-${take(suffix, 6)}'
  location: location
  sku: { name: 'Standard_B2s', tier: 'Burstable' }
  properties: {
    version: '16'
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    authConfig: { activeDirectoryAuth: 'Enabled', passwordAuth: 'Disabled', tenantId: subscription().tenantId }
    // Data, backups and WAL are AES-256 encrypted at rest with service-managed keys.
    // For a customer-managed key add dataEncryption { type: 'AzureKeyVault', primaryKeyURI, primaryUserAssignedIdentityId }.
  }
}

resource pgAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: pg
  name: pgEntraAdminObjectId
  properties: {
    principalName: pgEntraAdminName
    principalType: pgEntraAdminType
    tenantId: subscription().tenantId
  }
}

// Server parameters are applied one at a time.
resource pgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: pg
  name: 'azure.extensions'
  properties: { value: 'VECTOR', source: 'user-override' }
  dependsOn: [ pgAdmin ]
}

resource pgTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: pg
  name: 'require_secure_transport'
  properties: { value: 'ON', source: 'user-override' }
  dependsOn: [ pgExtensions ]
}

resource pgTlsVersion 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: pg
  name: 'ssl_min_protocol_version'
  properties: { value: 'TLSv1.2', source: 'user-override' }
  dependsOn: [ pgTls ]
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: 'hr_rag'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [ pgTlsVersion ]
}

// Only the Container Apps egress IP (and optionally your IP) can reach PostgreSQL.
resource pgFwEgress 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'container-apps-egress'
  properties: { startIpAddress: egressIp.properties.ipAddress, endIpAddress: egressIp.properties.ipAddress }
  dependsOn: [ pgDb ]
}

resource pgFwClient 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!empty(pgClientIp)) {
  parent: pg
  name: 'admin-client'
  properties: { startIpAddress: pgClientIp, endIpAddress: pgClientIp }
  dependsOn: [ pgFwEgress ]
}

// ---------- Storage (handbook uploads for the ingestion job) ----------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${prefix}st${take(suffix, 8)}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  properties: {
    isHnsEnabled: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    encryption: {
      requireInfrastructureEncryption: true   // double encryption at rest
      keySource: 'Microsoft.Storage'
      services: { blob: { enabled: true }, file: { enabled: true } }
    }
  }
}

resource landing 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: '${storage.name}/default/landing'
}

resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, pipelinesId.id, blobReader)
  properties: {
    principalId: pipelinesId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReader)
  }
}

// ---------- Container registry ----------
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}acr${take(suffix, 8)}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource acrRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: acr
  name: guid(acr.id, i == 0 ? apiId.id : pipelinesId.id, acrPull)
  properties: {
    principalId: i == 0 ? apiId.properties.principalId : pipelinesId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPull)
  }
}]

// ---------- Container Apps environment (VNet, NAT egress) ----------
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    vnetConfiguration: { infrastructureSubnetId: vnet.properties.subnets[0].id, internal: false }
    workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var pgEnv = [
  { name: 'PG_HOST', value: pg.properties.fullyQualifiedDomainName }
  { name: 'PG_DATABASE', value: 'hr_rag' }
  { name: 'PG_SSLMODE', value: 'verify-full' }
]
var registries = [ { server: acr.properties.loginServer, identity: apiId.id } ]
var pipelineRegistries = [ { server: acr.properties.loginServer, identity: pipelinesId.id } ]

// ---------- API + website ----------
resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${prefix}-api'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${apiId.id}': {} } }
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false            // HTTP is redirected to HTTPS; TLS 1.2+ terminates at ingress
      }
      registries: registries
      secrets: concat(
        [ kvRef(kv.properties.vaultUri, 'pii-encryption-key', apiId.id) ],
        authEnabled ? [ kvRef(kv.properties.vaultUri, 'entra-auth-client-secret', apiId.id) ] : []
      )
    }
    template: {
      containers: [ {
        name: 'api'
        image: '${acr.properties.loginServer}/pto-api:${imageTag}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(pgEnv, [
          { name: 'AZURE_CLIENT_ID', value: apiId.properties.clientId }   // DefaultAzureCredential -> UAI
          { name: 'PG_USER', value: apiId.name }                          // Entra principal name in PostgreSQL
          { name: 'PII_ENCRYPTION_KEY', secretRef: 'pii-encryption-key' }
          { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
          { name: 'CHAT_DEPLOYMENT', value: chatDeployment }
          { name: 'EMBEDDING_DEPLOYMENT', value: embeddingDeployment }
          { name: 'PTO_AUTH_MODE', value: authEnabled ? 'easyauth' : 'disabled' }
          { name: 'ALLOWED_ORIGINS', value: allowedOrigins }
        ])
        probes: [ { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 } } ]
      } ]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
  dependsOn: [ kvRoles, kvSecrets, authSecret, acrRoles ]
}

// Entra ID sign-in. Anonymous requests still reach the static site; the API itself
// rejects /pto without a signed-in principal holding the PTO.Read app role.
resource apiAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (deployApps && authEnabled) {
  parent: api
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: { unauthenticatedClientAction: 'AllowAnonymous' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraAuthClientId
          clientSecretSettingName: 'entra-auth-client-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${subscription().tenantId}/v2.0'
        }
      }
    }
  }
}

// ---------- Pipelines ----------
var pipelineEnv = concat(pgEnv, [
  { name: 'AZURE_CLIENT_ID', value: pipelinesId.properties.clientId }
  { name: 'PG_USER', value: pipelinesId.name }
])

resource employeeSync 'Microsoft.App/jobs@2024-03-01' = if (deployApps) {
  name: '${prefix}-employee-sync'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${pipelinesId.id}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: { cronExpression: '15 * * * *', parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 1800
      replicaRetryLimit: 2
      registries: pipelineRegistries
      secrets: [
        kvRef(kv.properties.vaultUri, 'pii-encryption-key', pipelinesId.id)
        kvRef(kv.properties.vaultUri, 'databricks-aws-client-secret', pipelinesId.id)
      ]
    }
    template: {
      containers: [ {
        name: 'sync'
        image: '${acr.properties.loginServer}/pto-pipelines:${imageTag}'
        command: [ 'python', 'sync_employees.py' ]
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(pipelineEnv, [
          { name: 'PII_ENCRYPTION_KEY', secretRef: 'pii-encryption-key' }
          { name: 'DATABRICKS_AWS_HOST', value: databricksAwsHost }
          { name: 'DATABRICKS_AWS_HTTP_PATH', value: databricksAwsHttpPath }
          { name: 'DATABRICKS_AWS_CLIENT_ID', value: databricksAwsClientId }
          { name: 'DATABRICKS_AWS_CLIENT_SECRET', secretRef: 'databricks-aws-client-secret' }
        ])
      } ]
    }
  }
  dependsOn: [ kvRoles, kvSecrets, acrRoles ]
}

resource handbookIngest 'Microsoft.App/jobs@2024-03-01' = if (deployApps) {
  name: '${prefix}-handbook-ingest'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${pipelinesId.id}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 1800
      replicaRetryLimit: 1
      registries: pipelineRegistries
    }
    template: {
      containers: [ {
        name: 'ingest'
        image: '${acr.properties.loginServer}/pto-pipelines:${imageTag}'
        command: [ 'python', 'ingest_handbook.py', '--blob-prefix', 'handbook/' ]
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(pipelineEnv, [
          { name: 'STORAGE_ACCOUNT', value: storage.name }
          { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
          { name: 'EMBEDDING_DEPLOYMENT', value: embeddingDeployment }
        ])
      } ]
    }
  }
  dependsOn: [ kvRoles, acrRoles, blobRole ]
}

output apiUrl string = deployApps ? 'https://${api!.properties.configuration.ingress.fqdn}' : ''
output acrLoginServer string = acr.properties.loginServer
output keyVaultName string = kv.name
output pgHost string = pg.properties.fullyQualifiedDomainName
output storageAccount string = storage.name
output egressIpAddress string = egressIp.properties.ipAddress   // add to the Databricks IP access list
output apiIdentityName string = apiId.name
output pipelinesIdentityName string = pipelinesId.name
output apiIdentityPrincipalId string = apiId.properties.principalId
output pipelinesIdentityPrincipalId string = pipelinesId.properties.principalId
