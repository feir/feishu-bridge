import Foundation

/// Validates Feishu App credentials by calling the tenant_access_token API.
enum CredentialValidator {

    private static let tokenURL = URL(
        string: "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
    )!

    struct Result {
        let valid: Bool
        let error: String?
    }

    /// Verify App ID + Secret against the Feishu API.
    /// Returns immediately with a result — does not throw.
    static func validate(appId: String, appSecret: String) async -> Result {
        let body: [String: String] = ["app_id": appId, "app_secret": appSecret]

        guard let bodyData = try? JSONSerialization.data(withJSONObject: body) else {
            return Result(valid: false, error: "Failed to encode request")
        }

        var request = URLRequest(url: tokenURL)
        request.httpMethod = "POST"
        request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        request.httpBody = bodyData
        request.timeoutInterval = 10

        do {
            let (data, response) = try await URLSession.shared.data(for: request)

            guard let http = response as? HTTPURLResponse else {
                return Result(valid: false, error: "Invalid response")
            }

            guard http.statusCode == 200 else {
                return Result(valid: false, error: "HTTP \(http.statusCode)")
            }

            // Parse response — look for "code": 0 (success)
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let code = json["code"] as? Int {
                if code == 0 {
                    return Result(valid: true, error: nil)
                } else {
                    let msg = json["msg"] as? String ?? "Unknown error"
                    return Result(valid: false, error: "Code \(code): \(msg)")
                }
            }

            return Result(valid: false, error: "Unexpected response format")

        } catch {
            return Result(valid: false, error: error.localizedDescription)
        }
    }
}
