pipeline {
  agent any
  stages {
    stage('Build') {
      parallel {
        stage('Build aarch64') {
          agent {
            node {
              label 'aarch64'
            }

          }
          steps {
            withDockerRegistry([ credentialsId: "fcf9c294-b8a9-4f7e-87d6-d0446f712411", url: "https://index.docker.io/v1/" ]) {
              sh 'ci_scripts/push_containers.sh'
            }
          }
        }
        stage('Build x86') {
          agent {
            node {
              label 'x86_64'
            }

          }
          steps {
            withDockerRegistry([ credentialsId: "fcf9c294-b8a9-4f7e-87d6-d0446f712411", url: "https://index.docker.io/v1/" ]) {
              sh 'ci_scripts/push_containers.sh'
            }
          }
        }
      }
    }
    stage('Push Manifest') {
      steps {
        withDockerRegistry([ credentialsId: "fcf9c294-b8a9-4f7e-87d6-d0446f712411", url: "https://index.docker.io/v1/" ]) {
          sh 'ci_scripts/push_manifest.sh'
        }
      }
    }
  }
}
