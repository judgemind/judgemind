import { gql } from '@apollo/client';

export const LOGIN_MUTATION = gql`
  mutation Login($email: String!, $password: String!) {
    login(email: $email, password: $password) {
      accessToken
      user {
        id
        email
        emailVerified
        displayName
        role
        createdAt
      }
    }
  }
`;

export const REGISTER_MUTATION = gql`
  mutation Register($email: String!, $password: String!, $displayName: String) {
    register(email: $email, password: $password, displayName: $displayName) {
      accessToken
      user {
        id
        email
        emailVerified
        displayName
        role
        createdAt
      }
    }
  }
`;

export const VERIFY_EMAIL_MUTATION = gql`
  mutation VerifyEmail($token: String!) {
    verifyEmail(token: $token)
  }
`;

export const LOGOUT_MUTATION = gql`
  mutation Logout {
    logout
  }
`;

export const ME_QUERY = gql`
  query Me {
    me {
      id
      email
      emailVerified
      displayName
      role
      createdAt
    }
  }
`;

export interface AuthUser {
  id: string;
  email: string;
  emailVerified: boolean;
  displayName: string | null;
  role: string;
  createdAt: string;
}

export interface AuthPayload {
  accessToken: string;
  user: AuthUser;
}
